"""Choose each listing's one good photo (user 2026-11-01: one very good image per listing is enough; it gives every view).
The steps stone_pipeline.photos.process calls in order; hosted calls only, no model runs here. Two run tags take part: the
standardiser's run over the scraped photos and its run over the copies a hosted edit model cleaned. Every answer is
stored next to the image it is about, so a re-run reads it back and never pays twice; one listing's failure is
reported and never stops the others.
  judge   per listing, its finished photos in its own order (the scraped run first, then the cleaned run) go to the
          judge until one passes, at most JUDGE_MAX_PER_LISTING per tag; the verdict is kept at <tag>/<src>/judged/.
          A slab listing prefers a whole slab (a result with <sha>.slab.png, which gets a 3D slab): after a close-up
          passes, only its whole results go on to the judge, and the first of those that passes is chosen instead
  clean   per listing with no pass, the first CLEAN_PHOTOS_PER_LISTING photos a cleaned copy can fix (failed only for
          reflections or marks, or held by the retouch or logo step) are edited by each CLEAN_MODELS model in turn until
          a copy passes the clean check; that copy becomes <clean tag>/<src>/cleaned/<sha>.jpg
  report  from the stored answers only: each listing's choice record (<clean tag>/chosen/<listing>.json, what publish
          reads) and <clean tag>/tracked.csv (this run's listings with no good photo, what happened to each photo;
          the process works every listing without a choice, so this is the environment's whole review list)
"""

from __future__ import annotations

import copy
import csv
import functools
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from botocore.exceptions import ClientError

from stone_pipeline.photos.publish import MISSING, chosen_prefix, excluded, pool_map
from stone_pipeline.photos.source import (LOGO_STEP, REDRAW, candidate_prefix, candidate_rule, discard, noted, promote,
                                       remove_candidate, round_prefix, undecided)
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import clean, judge

_SHA = re.compile(r"[0-9a-f]{64}")


class SpendCapReached(RuntimeError):
    """The process budget is reached: paid work of the whole run ends (the report still runs)."""
    WHAT = "budget"


class CleanCapReached(SpendCapReached):
    """The cleaning cap is reached: only the clean step ends; the judge and the GPU go on within the process budget."""
    WHAT = "cleaning cap"


class Halted(RuntimeError):
    """The run was stopped: no hosted call may start any more."""


class Spend:
    """Spend against the month's budget (hosted calls and GPU rounds): `before` is what the month's other runs spent
    (others(), summed again at every settle, so a run written since is never missed), then this run's. A price is
    reserved before the call or round and settled to what it cost, so parallel calls can never together pass the
    budget. save(total, uses) keeps this run's total after every change, a reserve counting as spent until it is settled,
    so a run that dies leaves an upper bound of what it spent, never too little; uses are the paid calls the run listed
    (record), kept by the same write. Storage is never asked while the spend's
    lock is held (a slow write never blocks the other calls), and a stop halts it without taking that lock (a SIGTERM
    may arrive while it is held)."""

    def __init__(self, budget, others=None, save=None, cap_error=SpendCapReached):
        self.budget, self.others, self.save, self.cap_error = budget, others, save, cap_error
        self.before = others() if others else 0.0
        self.spent, self.reserved, self.capped = 0.0, 0.0, False
        self.uses = []
        self._halt, self._why = threading.Event(), ""
        self._lock, self._io = threading.Lock(), threading.Lock()
        self._version = self._saved = 0               # changes made / the change the stored total is of

    def halt(self, why="the run is stopping"):
        """No call may start any more (a stop, the end of the work window, a lost lock): listings already running stop at
        their next call."""
        self._why = why
        self._halt.set()

    def left(self):
        """What the month's budget still covers, reserves counted."""
        with self._lock:
            return self.budget - self.before - self.spent - self.reserved

    def reserve(self, usd):
        with self._lock:
            if self._halt.is_set():
                raise Halted(self._why)
            if self.before + self.spent + self.reserved + usd > self.budget:
                raise self._refusal()
            self.reserved += usd
            self._version += 1
        self._keep()

    def refuse(self):
        """The cap's error for work the budget left cannot cover, the budget marked as reached; nothing is reserved (a
        GPU round with not one slice that fits: stone_pipeline.photos.batch.run_round)."""
        with self._lock:
            return self._refusal()

    def _refusal(self):
        self.capped = True
        return self.cap_error(f"spend would pass the month's {self.cap_error.WHAT} of ${self.budget:.2f} "
                              f"(spent ${self.before + self.spent:.2f})")

    def settle(self, reserved, cost):
        with self._lock:
            self.reserved -= reserved
            self.spent += cost
            self._version += 1
        if self.others:
            before = self.others()
            with self._lock:
                self.before = before
        self._keep()

    def paid_calls(self):
        """How many of the listed calls were paid for (a refused one is listed at $0 and is none)."""
        with self._lock:
            return sum(1 for use in self.uses if not use.get("refused"))

    def record(self, use):
        """List one paid call (its fields as given, stamped with when it was paid), kept with the run's total."""
        with self._lock:
            self.uses.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **use})
            self._version += 1
        self._keep()

    def _keep(self):
        """Store this run's total, outside the spend's lock: one write at a time, and only a total newer than the one
        stored, so overlapping calls can never leave an older total last."""
        if self.save is None:
            return
        with self._io:
            with self._lock:
                total, uses, version = self.spent + self.reserved, list(self.uses), self._version
            if version > self._saved:
                self.save(total, uses)
                self._saved = version


class _Locks:
    """One lock per photo: a photo several listings share is judged and cleaned once, never by two threads at once."""

    def lock(self, sha):
        with self._guard:
            return self._locks.setdefault(sha, threading.Lock())


class Candidates(_Locks):
    """The candidates of one run folder (`out`, its keys relative to it): what a recheck drew of whole slabs this
    version draws differently (stone_pipeline.photos.source.candidate_prefix), named as a run folder's results are, so
    verdict judges one as it judges a result; complete: those whose completion marker is written."""

    def __init__(self, out, keys):
        self.out, self.prefix = out, candidate_prefix(out)
        inner = [k[len(REDRAW):] for k in keys if k.startswith(REDRAW)]
        self.complete = {k[:64] for k in inner if k.endswith(".done.json") and "/" not in k and _SHA.match(k)}
        self.judged = _shas(inner, "judged/")
        self._locks, self._guard = {}, threading.Lock()
        self.asked = set()                             # the photos the judge was asked about this run


def read_candidates(client, out):
    """The Candidates of one run folder `out`, its candidate folder alone listed."""
    pages = client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=candidate_prefix(out))
    return Candidates(out, [o["Key"][len(out):] for page in pages for o in page.get("Contents", [])])


# Where a run folder keeps what it holds of one photo: <sha>.<view> at its top, then each folder by <sha>. Round reads
# exactly these, so a Round of a few photos (shas) lists each of them here and nothing else.
ROUND_FOLDERS = ("", "noslab/", LOGO_STEP, C.OUTLINE_CHECKED, "held/", "judged/", "cleaned/", "attempts/", REDRAW,
                 f"{REDRAW}judged/")


class Round(_Locks):
    """What one run tag holds for one source, its keys listed once: finished photos, the whole slabs among them, held
    ones, verdicts, cleaned copies and clean attempts ("<sha>.<model>": its check, its edited copy, its stored hosted
    request), and the candidates of an outline recheck (redraw)."""

    def __init__(self, client, tag, src, shas=None):
        """shas: read only what the folder holds of these photos (their keys under each of ROUND_FOLDERS, listed a few
        at a time), the same sets the whole folder gives restricted to them: a check about one listing never lists the
        source's whole run folder."""
        self.prefix = round_prefix(tag, src)
        prefixes = [self.prefix] if shas is None else [f"{self.prefix}{folder}{sha}" for sha in sorted(set(shas))
                                                       for folder in ROUND_FOLDERS]
        listed = pool_map(C.STORE_WORKERS, lambda prefix: [
            o["Key"][len(self.prefix):] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])], prefixes)
        keys = [k for part in listed for k in part]
        self.finished = _finished(keys)
        self.slabs = {k[:64] for k in keys if k.endswith(".slab.png") and "/" not in k}     # whole slabs: a 3D slab each
        self.studio = {k[:64] for k in keys if k.endswith(".studio.jpg") and "/" not in k}
        self.noslab = _shas(keys, "noslab/")
        self.logo_step = _shas(keys, LOGO_STEP)        # made by the retired logo step: never completed
        self.checked = _shas(keys, C.OUTLINE_CHECKED)  # whole slabs drawn under the current outline rule
        self.redraw = Candidates(self.prefix, keys)
        self.redrawn = self.redraw.complete            # whole slabs whose candidate waits for the judge (settle)
        self.held = _shas(keys, "held/")
        self.judged = _shas(keys, "judged/")
        self.cleaned = _shas(keys, "cleaned/")
        self.requested = _attempts(keys, ".request.json")
        self.edited = _attempts(keys, ".jpg")
        self.attempts = _attempts([k for k in keys if not k.endswith(".request.json")], ".json")
        self._locks, self._guard = {}, threading.Lock()
        self.asked = set()                             # the photos the judge was asked about this run

    @functools.cached_property
    def older_outline(self):
        """The whole slabs made under an older outline rule (no note of C.OUTLINE_CHECKED, no candidate waiting for the
        judge), not made by the retired logo step: the GPU checks each once (stone_pipeline.photos.source.rework)."""
        return (self.finished & self.slabs) - self.checked - self.redrawn - self.held - self.logo_step

    @functools.cached_property
    def unchecked(self):
        """The whole slabs whose outline recheck is not settled (older_outline, or a candidate waiting for the judge):
        not finished, so never judged before it is."""
        return self.older_outline | self.redrawn

    @functools.cached_property
    def outdated(self):
        """The results the GPU works on again, once each (stone_pipeline.photos.source.rework): those made before an open
        book's halves had their slab (a side view, no slab, not tried again, not settled by an outline recheck, none
        waiting for the judge), and the older_outline ones; never one the retired logo step made. Worked out once per
        Round: settle reads the rounds again after it changes them."""
        return ((self.finished & self.studio) - self.slabs - self.noslab - self.held - self.logo_step - self.checked
                - self.redrawn) | self.older_outline


class _Read(frozenset):
    """The members of a set read for some photos only (its scope): asked about another photo it raises LookupError, so
    a question nobody read the answer to is never a silent no."""

    def __new__(cls, members, scope):
        out = super().__new__(cls, members)
        out.scope = frozenset(scope)
        return out

    def __contains__(self, sha):
        if sha not in self.scope:
            raise LookupError(f"photo {sha[:12]} was not read")
        return frozenset.__contains__(self, sha)


class Results:
    """What a run folder holds of a few photos for the decisions applier's checks (decisions.refusals): prefix;
    finished (a "use" needs a result) of `shas`; judged and held (a "clean" reads its verdict or hold) of `verdicts`
    alone. The same answers a Round gives, for those photos only: asked about another it raises (_Read), and nothing
    else is answered (AttributeError)."""

    def __init__(self, client, tag, src, shas, verdicts):
        self.prefix = round_prefix(tag, src)
        shas, verdicts = set(shas) | set(verdicts), set(verdicts)
        prefixes = ([f"{self.prefix}{sha}" for sha in sorted(shas)]
                    + [f"{self.prefix}{folder}{sha}" for sha in sorted(verdicts) for folder in ("judged/", "held/")])
        listed = pool_map(C.STORE_WORKERS, lambda prefix: [
            o["Key"][len(self.prefix):] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])], prefixes)
        keys = [k for part in listed for k in part]
        self.finished = _Read(_finished(keys), shas)
        self.judged, self.held = _Read(_shas(keys, "judged/"), verdicts), _Read(_shas(keys, "held/"), verdicts)



def _finished(keys):
    """The photos with a result: <sha>.main.jpg at the folder's top."""
    return {k[:64] for k in keys if k.endswith(".main.jpg") and "/" not in k}


def _attempts(keys, suffix):
    """{"<sha>.<model>"} of the attempt files ending in suffix."""
    return {k[len("attempts/"):-len(suffix)] for k in keys if k.startswith("attempts/") and k.endswith(suffix)}


def _shas(keys, folder):
    return {m.group(0) for k in keys if k.startswith(folder) and (m := _SHA.match(k[len(folder):]))}


def _get(client, key):
    return client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def _get_json(client, key):
    return json.loads(_get(client, key))


def _put(client, key, body, content_type):
    client.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=content_type)


def _put_json(client, key, value):
    _put(client, key, json.dumps(value, sort_keys=True).encode(), "application/json")


def _original(client, src, sha):
    return _get(client, f"{imagestore.scraped_prefix(src)}{sha}.jpg")


def verdict(client, rnd, src, sha, fmt, spend=None):
    """The judge's stored verdict on this tag's result for the photo; judged now when there is none and spend is given,
    None when there is none and none may be bought."""
    key = f"{rnd.prefix}judged/{sha}.json"
    with rnd.lock(sha):
        stored = _get_json(client, key) if sha in rnd.judged else None
        if stored is not None and (judge.current(stored) or spend is None or sha in rnd.asked):
            return stored                              # an outdated or unparsed one is asked again, once a run
        if stored is None and spend is None:
            return None
        original, result = _original(client, src, sha), _get(client, f"{rnd.prefix}{sha}.main.jpg")   # read before paying
        request = judge.judging(original, result, fmt)   # prepared before anything is paid: a failure here is free
        found, cost = paid(spend, C.JUDGE_PRICE_USD, lambda: judge.judge(request), f"judge {src} {sha[:12]}")
        rnd.asked.add(sha)
        if found["verdict"] == "unparsed":             # counted, so an answer that never parses ends as a named fail
            found = {**found, "asks": (stored or {}).get("asks", 0) + 1 if (stored or {}).get("verdict") == "unparsed" else 1}
        _put_json(client, key, {**found, "cost": cost})
        rnd.judged.add(sha)
        return found


def paid(spend, price, call, what, use=None):
    """(answer, cost) of one hosted call returning (answer, cost): its price reserved first, then settled (settled)."""
    spend.reserve(price)
    return settled(spend, price, call, what, use)


def refused(exc):
    """Why a hosted call cost nothing, or None: its status when the service refused it before any work
    (HOSTED_REFUSED_CODES: a model refusing the photo as much as an auth, balance or rate refusal), or the error's name
    when the request never reached the service (no connection made). Any other failure may have been billed."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in C.HOSTED_REFUSED_CODES:
        return exc.response.status_code
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return type(exc).__name__
    return None


def settled(spend, price, call, what, use=None):
    """(answer, cost) of one hosted call whose price is already reserved, settled to what it cost. A call refused before
    any work (refused: HOSTED_REFUSED_CODES) costs nothing. Any other failure may have been billed (sent, then lost),
    and an answer without its cost has one: both count at the reserved price, never as free. With `use`, the call is
    listed in the spend (record) at what it counted, a failed or refused one included."""
    try:
        answer, cost = call()
    except BaseException as exc:
        code = refused(exc)
        spend.settle(price, 0.0 if code else price)
        if use is not None:
            spend.record({**use, "usd": 0.0, "refused": code} if code else {**use, "usd": price, "failed": True})
        raise
    if cost is None:
        print(f"   WARNING: {what}: the answer carries no usage.cost; counted at the reserved ${price:.2f}", flush=True)
        cost = price
    spend.settle(price, cost)
    if use is not None:
        spend.record({**use, "usd": cost})
    return answer, cost

def candidates(listing, exclude):
    """The photos a listing is judged on: its first JUDGE_MAX_PER_LISTING photos, excluded ones left out first. The GPU
    works exactly these (Selection.gpu_todo) and the judge looks at exactly these, so the two never differ."""
    return [sha for sha in listing["photos"] if sha not in exclude][:C.JUDGE_MAX_PER_LISTING]


def choose(client, rounds, listing, exclude, spend=None, owner=None):
    """((tag, sha) of the listing's photo to publish or None, whether the answer is final): the owner's choice when it has
    one (Selection.owner_pick: it overrides the judge, nothing of the listing is judged), else the standardiser's
    results of its candidates in order, then its cleaned copies' results, the first the judge passes. A slab listing
    prefers a whole slab (its result has a 3D slab): a passing close-up is the answer only while no whole slab passes,
    and once one is had (listing["kept"]: the choice a run made before, or the first close-up passed here) only whole
    results are judged. A photo held by the standardiser is skipped; a photo with no result yet, or no verdict yet,
    leaves the answer open (never read as "no good photo", which would send the listing to the hosted edit or the
    tracked list). rounds = [(tag, {source: Round})], the scraped run first, then (optionally) the cleaned run; exclude =
    the photos never chosen by themselves (Selection.blocked)."""
    if owner is not None:
        return owner, True
    src = listing["source"]
    whole_wanted = listing["format"] == C.CUTOUT_FORMAT
    fallback, complete = listing.get("kept"), True
    for i, (tag, rnd) in enumerate(rounds):
        r = rnd[src]
        shas = candidates(listing, exclude) if i == 0 else [sha for sha in listing["photos"] if sha in r.cleaned and sha not in exclude]
        for sha in shas:
            if sha in r.held:
                continue
            if sha not in r.finished or sha in r.unchecked:     # no result yet, or its outline recheck not settled
                complete = False
                continue
            whole = sha in r.slabs
            if fallback is not None and not whole:      # another close-up: no better than the pass already had
                continue
            found = verdict(client, r, src, sha, listing["format"], spend)
            if found is None or not judge.current(found):    # no verdict, or a fail under an older bar: not final
                complete = False
            elif judge.passed(found):
                if whole or not whole_wanted:
                    return (tag, sha), True
                fallback = (tag, sha)
    return fallback, complete

def fixable(client, rnd, sha, exclude):
    """What a cleaned copy of this photo would fix, or None when it could not pass: {"kinds": [...]} when the judge failed
    its result only for reflections, marks or fills, {"held": reason} when the retouch or logo step held it. A photo held
    for its outline, or never judged, is not worth an edit."""
    if sha in exclude:
        return None
    if sha in rnd.finished:
        if sha not in rnd.judged:
            return None
        found = _get_json(client, f"{rnd.prefix}judged/{sha}.json")
        if judge.current(found) and judge.fixable(found):            # an outdated fail is judged again first
            return {"kinds": sorted({p["kind"] for p in found["problems"]})}
        return None
    if sha in rnd.held:
        reason = _get_json(client, f"{rnd.prefix}held/{sha}.json")["reason"]
        return {"held": reason} if reason.startswith(C.CLEAN_FIXES_HELD) else None
    return None


def authorised(cause, sha, click):
    """Why this fixable photo may be sent to a paid edit, or None (owner 2026-11-05): "click" when the owner's valid click
    for the listing names this photo (always, within the cleaning cap, whatever the clean switch says); "auto" for a mark
    on the stone (CLEAN_AUTO_KINDS, CLEAN_AUTO_HELD; a mark with a reflection besides is cleaned too, the edit removes
    both) while the clean switch (CLEAN_ENABLED) is on. Any other fixable photo waits for a click."""
    if click is not None and click["sha"] == sha:
        return "click"
    if C.CLEAN_ENABLED and (set(cause.get("kinds", ())) & set(C.CLEAN_AUTO_KINDS)
                            or cause.get("held", "").startswith(C.CLEAN_AUTO_HELD)):
        return "auto"
    return None


def click_problem(client, rnd, listing, click, exclude):
    """Why the owner's click for this listing can never be used, or None: its record (record_problem), or a photo whose
    current verdict or hold a cleaned copy cannot fix (unfixable). The one rule the decisions applier refuses a clean
    by; a photo not judged yet (its GPU result, its outline recheck or its verdict to come) is no problem: the click is
    stored and the run reports it blocked until its listing is fully judged (click_state)."""
    return record_problem(listing, click) or (CANNOT_FIX if unfixable(client, rnd, click["sha"], exclude) else None)


def unfixable(client, rnd, sha, exclude):
    """Whether a cleaned copy of this photo can never pass, by what is known now: an excluded photo, a held one whose
    hold an edit does not fix, or one whose current verdict fails for what an edit does not fix (or passes). A photo
    with no result, no verdict yet or an outdated one is not known to be unfixable."""
    if sha in exclude:
        return True
    if sha in rnd.finished:
        if sha not in rnd.judged:
            return False
        return judge.current(_get_json(client, f"{rnd.prefix}judged/{sha}.json")) and fixable(client, rnd, sha, exclude) is None
    return sha in rnd.held and fixable(client, rnd, sha, exclude) is None


CANNOT_FIX = "it names a photo a cleaned copy cannot fix"
NO_MODEL_LEFT = "no edit model left to try (each one failed or refused)"
PENDING = "the listing is not fully judged yet (a photo awaits its GPU result, its outline recheck or its verdict)"


def record_problem(listing, click):
    """Why the click record itself cannot be used for this listing, or None: its file could not be read, it has no
    time, or it names another SKU, listing or photo than the listing's."""
    if "error" in click:
        return f"its file cannot be read: {click['error']}"
    if not click.get("at"):
        return "it has no time (at)"
    if click.get("sku") != listing["sku"]:
        return f"it names SKU {click.get('sku')!r}, not its file's {listing['sku']!r}"
    if click.get("listing") != listing["key"]:
        return f"it names listing {click.get('listing')!r}, not {listing['key']!r}"
    if click.get("sha") not in listing["photos"]:
        return "it names a photo that is not the listing's"
    return None


def listing_state(client, rounds, listing, exclude, owner):
    """Where a listing stands for the clean step, from its scraped photos' stored answers (rounds[:1], choose): "owner"
    (the owner chose its photo), "passes" (one passes the judge), "pending" (not fully judged: a photo awaits its GPU
    result, its outline recheck or a current verdict) or "open" (fully judged, none passes: a cleaned copy may help)."""
    if owner is not None:
        return "owner"
    picked, complete = choose(client, rounds[:1], listing, exclude)
    return "passes" if picked is not None else "open" if complete else "pending"


def click_state(client, rounds, listing, exclude, click, state):
    """(state, why) of the owner's click on the listing, or None without one: the one answer the clean step (clean_plan
    sends a click only when it is "ready"), the run's problems and the review list (report) read. `state` is the
    listing's (listing_state). "blocked" with why it cannot be used now (its record, record_problem; the owner chose a
    photo; the listing not fully judged yet; its photo one a cleaned copy cannot fix; no edit model left to try),
    "cleaned" (its copy is made and goes through the GPU and the judge), "done" (a photo of the listing passes), or
    "ready". Waiting for a budget is not a state of the click: the run's admission says so (stone_pipeline.photos.process)."""
    if click is None:
        return None
    if state == "owner":
        return "blocked", "the owner chose a photo for this listing"
    if problem := record_problem(listing, click):
        return "blocked", problem
    (_, gpu), (_, cl) = rounds
    src = listing["source"]
    if click["sha"] in cl[src].cleaned:
        return "cleaned", None
    if state == "passes":
        return "done", "a photo of the listing passes"
    if state == "pending":
        return "blocked", PENDING
    if fixable(client, gpu[src], click["sha"], exclude) is None:
        return "blocked", CANNOT_FIX
    if not models_left(client, cl[src], click["sha"]):
        return "blocked", NO_MODEL_LEFT
    return "ready", None


def click_line(click, state):
    """The click's state as one line, for the review list and the run's problems."""
    head = f"owner's click on {click['sha'][:12]}" if click.get("sha") else "owner's click"
    return f"{head}: {state[0]}" + (f": {state[1]}" if state[1] else "")


def _current(attempt):
    """Whether a stored attempt stands: it passed, or it failed under the current clean instruction (CLEAN_VERSION)."""
    return attempt.get("passes") or attempt.get("clean_version", 1) >= C.CLEAN_VERSION


def models_left(client, cl, sha):
    """The edit models not yet tried on this photo under the current instruction: none left, nothing more can be paid."""
    return [name for name in clean.MODELS
            if f"{sha}.{name}" not in cl.attempts or not _current(_get_json(client, f"{cl.prefix}attempts/{sha}.{name}.json"))]


def paid_unchecked(cl, sha):
    """The models whose edit of this photo is paid (requested or made) and not checked yet."""
    return [name for name in clean.MODELS
            if (ident := f"{sha}.{name}") not in cl.attempts and (ident in cl.requested or ident in cl.edited)]


def clean_photo(client, cl, src, sha, spend, listing, why, models=clean.MODELS):
    """Each of `models` in turn until a cleaned copy passes the clean check; that copy becomes cleaned/<sha>.jpg. Returns
    whether one passed. spend is the cleaning cap's; each paid call is listed there for the listing that asked for it,
    with why it was sent (`why`: its reason, auto, click or paid, and what the edit fixes)."""
    with cl.lock(sha):
        if sha in cl.cleaned:
            return True
        original = _original(client, src, sha)
        use = {"source": src, "sha": sha, "sku": listing["sku"], "listing": listing["key"], **why}
        for name in models:
            if _attempt(client, cl, sha, name, original, spend, use)["passes"]:
                client.copy_object(Bucket=S3_BUCKET, Key=f"{cl.prefix}cleaned/{sha}.jpg", ContentType="image/jpeg",
                                   CopySource={"Bucket": S3_BUCKET, "Key": f"{cl.prefix}attempts/{sha}.{name}.jpg"},
                                   MetadataDirective="REPLACE")
                cl.cleaned.add(sha)
                return True
        return False


def _attempt(client, cl, sha, name, original, spend, use):
    """The clean check of model `name`'s copy of the photo, made once. A stored check is read back, a stored copy is
    checked, a stored request is collected; only a photo never sent is sent, its price counted as soon as the queue
    accepts it (it is paid even if collecting it fails later). Nothing is ever paid twice: an edit is sent to a model
    once per photo. A send refused before any work (refused) costs nothing: a refusal of the photo for good (given_up)
    is a failed attempt, any other refusal is loud and sent again next run. A send that may have been billed and has no
    usable answer (a timeout, a 5xx), and a stored request the queue no longer has (404, 410), are failed attempts
    counted as paid: the next model gets its turn, the edit is never sent again. A request the queue still has but has
    not finished is collected again next run. A photo sent here has its edit and its clean check reserved together, so
    the cap never leaves a paid copy unchecked in the run that paid for it; the check's share is released when no check
    is made.
    Every paid call is listed (spend.record, with `use`)."""
    att, ident = f"{cl.prefix}attempts/{sha}.{name}", f"{sha}.{name}"
    if ident in cl.attempts:
        stored = _get_json(client, f"{att}.json")
        if _current(stored):
            return stored
        cl.edited.discard(ident)                       # failed under an older instruction: made again with the current one
        cl.requested.discard(ident)
    held = 0.0                                         # the check's price, reserved with an edit sent here
    try:
        if ident in cl.edited:
            copy = _get(client, f"{att}.jpg")
        else:
            if ident in cl.requested:
                job = _get_json(client, f"{att}.request.json")
            else:
                price = C.CLEAN_PRICE_USD[name]            # an edit is billed at its list price once the queue has it
                prepared = clean.prepare(name, original)   # before anything is reserved: a failure here is free and loud
                spend.reserve(price + C.JUDGE_PRICE_USD)
                held = C.JUDGE_PRICE_USD
                try:
                    job, _ = settled(spend, price, lambda: (clean.submit(prepared), price),
                                     f"edit {name} {sha[:12]}", {**use, "model": name, "call": "edit"})
                except Exception as exc:
                    if refused(exc) is None:           # may have been billed: counted, never sent again
                        return _failed(client, cl, att, ident, name,
                                       f"the edit got no usable answer ({type(exc).__name__}): counted as paid")
                    if (code := given_up(exc)) is None:
                        raise                          # refused before any work (auth, balance, rate, no connection): loud
                    return _failed(client, cl, att, ident, name, f"the edit was refused ({code})")
                _put_json(client, f"{att}.request.json", job)
                cl.requested.add(ident)
            try:
                copy = clean.collect(job)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (404, 410):     # a paid request's result is gone: never sent again
                    return _failed(client, cl, att, ident, name,
                                   f"its result could not be found ({exc.response.status_code}): counted as paid")
                if (code := given_up(exc)) is None:
                    raise                              # anything else (auth, balance, 5xx, 429): loud, collected again
                return _failed(client, cl, att, ident, name, f"the edit failed ({code})")
            except clean.NoImage as exc:
                return _failed(client, cl, att, ident, name, str(exc))
            _put(client, f"{att}.jpg", copy, "image/jpeg")
            cl.edited.add(ident)
        asked = judge.checking(original, copy)         # prepared before anything is paid: a failure here is free
        call, what = (lambda: judge.check_clean(asked)), f"clean check {name} {sha[:12]}"
        listed = {**use, "model": name, "call": "check"}
        reserved, held = held, 0.0
        check, cost = (settled(spend, reserved, call, what, listed) if reserved
                       else paid(spend, C.JUDGE_PRICE_USD, call, what, listed))
    finally:
        if held:                                       # no check was made: its share goes back to the cap
            spend.settle(held, 0.0)
    _put_json(client, f"{att}.json", {**check, "edit_model": name, "check_cost": cost, "clean_version": C.CLEAN_VERSION})
    cl.attempts.add(ident)
    return check


def given_up(exc):
    """The status when the edit model refused this photo for good (CLEAN_FAILED_CODES), at submit or at collect: the
    attempt is a failed one and the next model gets its turn; None for any other failure."""
    code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    return code if code in C.CLEAN_FAILED_CODES else None


def _failed(client, cl, att, ident, name, why):
    """A hosted edit that failed for good, recorded as a failed attempt: the next model gets its turn, now and on re-runs."""
    check = {"clean": False, "faithful": False, "passes": False, "problems": why, "edit_model": name, "check_cost": 0.0,
             "clean_version": C.CLEAN_VERSION}
    _put_json(client, f"{att}.json", check)
    cl.attempts.add(ident)
    return check


def cleaned_copies(cl, listing, click):
    """The cleaned copies (of the clean round `cl`) that are the listing's cleaned photo: of the clicked photo alone when
    the owner clicked one (the click's photo is the unit: a copy of another photo leaves the click to do), else of any
    of its photos (one photo per listing is cleaned)."""
    return [sha for sha in ([click["sha"]] if click is not None else listing["photos"]) if sha in cl.cleaned]


@dataclass(frozen=True)
class CleanPlan:
    """What the clean step does for one listing (clean_plan), read from the stores before anything is paid. `collect`:
    ((sha, models), ...) its edits already paid and not checked, collected and checked first (the payment is made,
    whatever the gate says now). `send`: ((sha, why, models), ...) its photos for new edits when no collected copy
    passes, each sent to its `models` in turn until a copy passes (cheapest first). pay does exactly this and never
    more, so clean_price is its worst case on the cleaning cap and the GPU round and judge of its one copy are priced
    by stone_pipeline.photos.batch.price."""
    listing: dict
    collect: tuple
    send: tuple
    clicked: str | None = None                          # the photo of the owner's ready click, when there is one

    @property
    def click(self):
        """Whether the plan does the owner's click: its clicked photo leads what it collects or sends (the owed check of
        a clicked photo's paid edit is the click's work too). Admission takes such a plan before the marks."""
        first = (self.collect or self.send or ((None,),))[0][0]
        return self.clicked is not None and first == self.clicked

    @property
    def sends_click(self):
        """Whether its new edit is the owner's click (so a wait of its send is the click's wait)."""
        return bool(self.send) and self.send[0][1]["reason"] == "click"


def clean_price(plan):
    """The most pay(plan) can take of the cleaning cap: a clean check per collected edit, and an edit at its list price
    with its check for every model it may send."""
    return (sum(len(models) for _, models in plan.collect) * C.JUDGE_PRICE_USD
            + sum(C.CLEAN_PRICE_USD[m] + C.JUDGE_PRICE_USD for _, _, models in plan.send for m in models))


def clean_plan(client, rounds, listing, exclude, owner=None, click=None):
    """The CleanPlan of a listing with no good photo from the standardiser, from stored answers only (nothing is paid or
    written), or None when there is nothing to clean: the owner chose its photo, it already has its cleaned copy
    (cleaned_copies: of the clicked photo when the owner clicked one; its result is judged later), or it is not fully
    judged or has a pass (choose). Its first CLEAN_PHOTOS_PER_LISTING fixable photos that may be paid for (authorised:
    the photo the owner's valid click names first, then a mark on the stone) with a model left to try are its sends,
    each to the models not tried yet (an edit already paid for is collected instead). `click` is the listing's click
    record as read; it is sent only when its state is "ready" (click_state)."""
    (_, gpu), (_, cl) = rounds
    src = listing["source"]
    rnd = cl[src]
    state = listing_state(client, rounds, listing, exclude, owner)
    if state == "owner":
        return None
    clicked = click_state(client, rounds, listing, exclude, click, state)
    if clicked is not None and clicked[0] == "cleaned":
        return None                                    # its copy goes on through the GPU and the judge
    click = click if clicked == ("ready", None) else None
    if state != "open" or cleaned_copies(rnd, listing, click):
        return None                                    # not fully judged, or a pass: the report counts it
    collect = tuple(sorted(((sha, models) for sha in listing["photos"]
                            if sha not in exclude and (models := paid_unchecked(rnd, sha))),
                           key=lambda item: click is None or item[0] != click["sha"]))   # the clicked photo first
    sent = [(sha, {"reason": why, **cause, **({"click_at": click["at"]} if why == "click" else {})})
            for sha in listing["photos"]
            if (cause := fixable(client, gpu[src], sha, exclude)) is not None and (why := authorised(cause, sha, click))]
    sent.sort(key=lambda item: item[1]["reason"] != "click")       # the owner's clicked photo first (stable otherwise)
    send = tuple((sha, why, models) for sha, why in sent
                 if (models := [m for m in models_left(client, rnd, sha) if m not in paid_unchecked(rnd, sha)])
                 )[:C.CLEAN_PHOTOS_PER_LISTING]
    return CleanPlan(listing, collect, send, click["sha"] if click else None) if collect or send else None


def pay(client, rounds, plan, spend):
    """The plan carried out on the cleaning cap (spend): its collected edits checked, then its photos sent to their
    models in turn; the first copy that passes is the listing's cleaned copy, so it stops there (clean_photo)."""
    src = plan.listing["source"]
    cl = rounds[1][1][src]
    for sha, models in plan.collect:
        if clean_photo(client, cl, src, sha, spend, plan.listing, {"reason": "paid"}, models):
            return
    for sha, why, models in plan.send:
        if clean_photo(client, cl, src, sha, spend, plan.listing, why, models):
            return


def photo_notes(client, rounds, listing, exclude, conflicts, click=None, state=None):
    """What happened to each of a listing's photos, for a person reading the tracked list, and the owner's click on it:
    its state (click_state, or waiting for a budget: the run's admission), click_line."""
    notes = []
    src = listing["source"]
    for sha in listing["photos"]:
        parts = []
        if (src, sha) in conflicts:
            partners = [k for k in conflicts[(src, sha)] if k != listing["key"]]
            parts.append(f"{C.CONFLICT_REASON}: also shown by {', '.join(partners)} (another variety or finish): "
                         "never chosen by itself, the owner assigns it")
        for tag, rnd in rounds:
            r = rnd[src]
            if sha in exclude:
                parts.append("excluded after review")
                break
            if sha in r.judged:
                v = _get_json(client, f"{r.prefix}judged/{sha}.json")
                kinds = ", ".join(sorted({p.get("kind", "?") for p in v.get("problems", [])}))
                parts.append(f"{tag}: the judge's answer could not be read" if v["verdict"] == "unparsed"
                             else f"{tag}: judge {v['verdict']}" + (f" ({kinds})" if kinds else ""))
            elif sha in r.finished:
                parts.append(f"{tag}: finished, not judged")
            elif sha in r.held:
                parts.append(f"{tag}: held: {_get_json(client, f'{r.prefix}held/{sha}.json')['reason']}")
            tried = [a for a in sorted(r.attempts) if a.startswith(sha)]
            if tried:
                parts.append("cleaned by " + ", ".join(a.split(".", 1)[1] for a in tried) + ("; a copy passed" if sha in r.cleaned else "; none passed"))
        notes.append(f"{sha[:12]}: " + ("; ".join(parts) or "no result"))
    if click is not None:
        notes.append(click_line(click, state))
    return " | ".join(notes)


# The hidden-listings page's reasons (review.json, GET /config/v1/photos/hidden): its contract, not a setting. The first
# seven come from a photos run's report (review_row); the last four only from the config API's join of the ledger's
# hidden listings with the photo stores (owner_removed, not_worked_yet, chosen_not_linked) and publish's tiles.
REVIEW_REASONS = ("not_fixable", "awaiting_auto", "awaiting_click", "clean_failed", "photo_conflict", "pending",
                  "excluded", "owner_removed", "tile_no_stone", "not_worked_yet", "chosen_not_linked")


def result_state(client, rnd, sha):
    """Where one photo's result stands in a round, from its stored answers (choose reads them in the same order): "held"
    (the standardiser held it), "no_result", "unchecked" (its outline recheck not settled), "not_judged", "unparsed"
    (the judge's answer could not be read: asked again), "outdated" (a verdict under an older bar: judged again),
    "pass" or "fail"."""
    if sha in rnd.held:
        return "held"
    if sha not in rnd.finished:
        return "no_result"
    if sha in rnd.unchecked:
        return "unchecked"
    if sha not in rnd.judged:
        return "not_judged"
    found = _get_json(client, f"{rnd.prefix}judged/{sha}.json")
    if found["verdict"] == "unparsed":
        return "unparsed"
    if not judge.current(found):
        return "outdated"
    return "pass" if judge.passed(found) else "fail"


def review_photo(client, rounds, listing, sha, exclude, blocked):
    """One photo of a listing on the review list: its scraped photo's result (result_state; "excluded" or "conflict"
    when it is never chosen by itself), the kinds of its verdict, its hold, whether a cleaned copy could fix it
    (fixable) and may be paid without a click (authorised "auto"), and its clean: the models tried and left (left only
    for a fixable photo) and its cleaned copy's result (None without one). exclude: the reviewed exclude list; blocked:
    Selection.blocked (the exclude list and the listing's conflict photos)."""
    (tag, gpu), (_, cl) = rounds
    g, c = gpu[listing["source"]], cl[listing["source"]]
    state = ("excluded" if sha in exclude else "conflict" if sha in blocked else result_state(client, g, sha))
    found = _get_json(client, f"{g.prefix}judged/{sha}.json") if sha in g.judged else None
    cause = fixable(client, g, sha, blocked)
    return {"sha": sha, "tag": tag, "state": state,
            "kinds": sorted({p.get("kind", "?") for p in found.get("problems", [])}) if found else [],
            "held": _get_json(client, f"{g.prefix}held/{sha}.json")["reason"] if sha in g.held else None,
            "fixable": cause is not None, "auto": cause is not None and authorised(cause, sha, None) == "auto",
            "clean": {"tried": [m for m in clean.MODELS if f"{sha}.{m}" in c.attempts],
                      "left": models_left(client, c, sha) if cause is not None else [],
                      "copy": result_state(client, c, sha) if sha in c.cleaned else None}}


def review_reason(listing, outcome, photos, state, blocked):
    """Why a listing the run did not give a good photo is hidden (REVIEW_REASONS), from its outcome (report), its photos
    (review_photo) and its click's state (click_state): "pending" while not fully judged; "excluded" or "photo_conflict"
    when none of its photos may be chosen by itself; "clean_failed" when its cleaned copy failed the judge or every
    fixable photo has no model left; "not_fixable" when no cleaned copy could fix any; "awaiting_auto" when a mark may
    still be cleaned without a click (authorised); else "awaiting_click"."""
    if outcome == "pending":
        return "pending"
    if listing["photos"] and not candidates(listing, blocked):
        return "photo_conflict" if any(p["state"] == "conflict" for p in photos) else "excluded"
    if (state is not None and state[0] == "cleaned") or any(p["clean"]["copy"] is not None for p in photos):
        return "clean_failed"
    fixes = [p for p in photos if p["fixable"]]
    if not fixes:
        return "not_fixable"
    if not any(p["clean"]["left"] for p in fixes):
        return "clean_failed"
    if any(p["auto"] and p["clean"]["left"] for p in fixes):
        return "awaiting_auto"
    return "awaiting_click"


def review_row(client, rounds, listing, outcome, exclude, blocked, click, state, mark_wait):
    """The listing's row on the review list (review.json): its reason (review_reason), why its marks wait (admission,
    only for "awaiting_auto"), the owner's click in its one state (click_state, "waiting" from admission) and its
    photos (review_photo). No URL: the config API makes those when it serves the list."""
    photos = [review_photo(client, rounds, listing, sha, exclude, blocked) for sha in listing["photos"]]
    reason = review_reason(listing, outcome, photos, state, blocked)
    return {"listing": listing["key"], "source": listing["source"], "format": listing["format"], "reason": reason,
            "why": mark_wait if reason == "awaiting_auto" else None,
            "click": None if click is None else {"sha": click.get("sha"), "at": click.get("at"), "state": state[0],
                                                 "why": state[1]},
            "photos": photos}


def read_rounds(client, tags, sources):
    """[(tag, {source: Round})] of the tags for the sources, each listed once."""
    return [(tag, {src: Round(client, tag, src) for src in sources}) for tag in tags]


def owner_choice(record, listing, exclude, conflicts, rounds):
    """(tag, sha) of the owner's choice record of a listing (stone_pipeline.photos.decisions; by SKU, so a re-bind that moves
    the handle keeps it) while it still applies, else None; the one check, every run. It applies while its photo is one
    of the listing's, not excluded, and the tag's result exists (a choice that can never be published is never picked
    again and again). A photo listings of another stone show (conflicts) is the listing's only by the owner's own
    decision on it: a choice that reached it through a sibling of its stone (decided_on another listing) assigns it
    nothing, as the stones may have changed since. Stored answers only: nothing is read or paid."""
    if record is None or record["sha"] not in listing["photos"] or record["sha"] in exclude:
        return None
    if (listing["source"], record["sha"]) in conflicts and record["decided_on"] != record["listing"]:
        return None
    by_tag = dict(rounds)
    if record["tag"] not in by_tag or record["sha"] not in by_tag[record["tag"]][listing["source"]].finished:
        return None
    return record["tag"], record["sha"]


class Selection:
    """The listings one run works on and what the two run tags hold for them (rounds: read_rounds, re-read after each
    GPU round). owner = {SKU: the owner's choice record} (stone_pipeline.photos.decisions); conflicts = {(source, sha):
    listing keys} of the photos listings of several stones show (index.shared_photos); chosen = [(tag, source, sha)] of
    what the listings a run may publish show now (stone_pipeline.photos.process.live_choices): an outdated one goes to the GPU
    too, its listing not picked again (its verdict and choice stand); clicks = {SKU: the owner's click to clean one
    photo} (publish.clicks_prefix); frozen = {(source, sha)} of the photos of the listings whose choice record cannot be
    read (stone_pipeline.photos.process): left exactly as published, so their pending redraws wait (settle)."""

    def __init__(self, client, listings, run_tag, clean_tag, owner, conflicts, rounds, chosen, clicks, frozen):
        if not run_tag or not clean_tag or run_tag == clean_tag:
            raise ValueError("two different run tags are required: the scraped photos' run and the cleaned copies' run")
        self.client, self.listings, self.tags = client, listings, (run_tag, clean_tag)
        self.owner, self.conflicts, self.rounds, self.chosen = owner, conflicts, rounds, sorted(set(chosen))
        self.clicks, self.frozen = clicks, frozen
        self.sources = sorted({l["source"] for l in listings} | {src for _, src, _ in self.chosen})
        self.exclude = excluded()
        self.root = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{clean_tag}/"
        self._asked = {}
        self._share_asked()

    def refresh(self):
        self.rounds = read_rounds(self.client, self.tags, self.sources)
        self._share_asked()

    def _share_asked(self):
        """Every round and candidate folder read again keeps what the judge was asked about earlier in this run (so a
        photo is asked at most once a run), one set per folder, shared by the selections narrowed from this one."""
        for _, by_source in self.rounds:
            for rnd in by_source.values():
                rnd.asked = self._asked.setdefault(rnd.prefix, rnd.asked)
                rnd.redraw.asked = self._asked.setdefault(rnd.redraw.prefix, rnd.redraw.asked)

    def only(self, listings, clicks):
        """This selection narrowed to `listings`, its clean step paying only `clicks` ({SKU: click}), over the same
        stores (the same rounds, so what one step stores the other reads); none of the live choices (they are the whole
        run's)."""
        narrowed = copy.copy(self)
        narrowed.listings, narrowed.clicks, narrowed.chosen = listings, clicks, []
        narrowed.sources = sorted({l["source"] for l in listings})
        return narrowed

    def plans(self):
        """([CleanPlan] of this selection's listings with something to clean, in sheet order (clean_plan), [(listing key,
        why its plan could not be read)]): read a few listings at a time; one listing's failed read never stops the
        others (the next run plans it again)."""
        def one(l):
            try:
                return clean_plan(self.client, self.rounds, l, self.blocked(l), self.owner_pick(l),
                                  self.clicks.get(l["sku"])), None
            except Exception as exc:                   # one listing never stops the others; the re-run retries it
                return None, (l["key"], f"{type(exc).__name__}: {exc}")
        found = pool_map(C.SELECT_WORKERS, one, self.listings)
        return [plan for plan, _ in found if plan is not None], [failure for _, failure in found if failure is not None]

    def click_state(self, listing):
        """(the listing's click or None, its click_state): as the clean step sees it."""
        click = self.clicks.get(listing["sku"])
        if click is None:
            return None, None
        state = listing_state(self.client, self.rounds, listing, self.blocked(listing), self.owner_pick(listing))
        return click, click_state(self.client, self.rounds, listing, self.blocked(listing), click, state)

    def owner_pick(self, listing):
        """(tag, sha) of the owner's choice for this listing while it applies (owner_choice), else None."""
        return owner_choice(self.owner.get(listing["sku"]), listing, self.exclude, self.conflicts, self.rounds)

    def blocked(self, listing):
        """The listing's photos never chosen by themselves: the reviewed exclude list and the photos it shares with a
        listing of another stone (a conflict: only the owner assigns those)."""
        return self.exclude | {sha for sha in listing["photos"] if (listing["source"], sha) in self.conflicts}

    def gpu_todo(self):
        """{source: shas} of each listing's candidates (the photos the judge looks at) the standardiser has not run yet,
        or whose result is outdated, none of a listing the owner chose a photo for; and the chosen results of this run
        tag that are outdated (excluded ones left out)."""
        gpu = self.rounds[0][1]
        picked = [(l["source"], sha) for l in self.listings if self.owner_pick(l) is None
                  for sha in candidates(l, self.blocked(l))]
        chosen = [(src, sha) for tag, src, sha in self.chosen if tag == self.tags[0] and sha not in self.exclude]
        return _by_source((src, sha) for src, sha in picked + chosen if needs_gpu(gpu[src], sha))

    def clean_todo(self):
        """{source: shas} of the cleaned copies of this run's listings' photos and of the chosen cleaned copies (excluded
        ones left out) the standardiser has not run yet, or whose result is outdated; never a cleaned copy of a photo
        no current listing has."""
        cl = self.rounds[1][1]
        wanted = ({sha for l in self.listings for sha in l["photos"]} |
                  {sha for tag, _, sha in self.chosen if tag == self.tags[1]}) - self.exclude
        return _by_source((src, sha) for src in self.sources for sha in cl[src].cleaned & wanted if needs_gpu(cl[src], sha))


def needs_gpu(rnd, sha):
    """Whether the GPU works on this photo: no result yet, or an outdated one (Round.outdated)."""
    return sha not in rnd.finished | rnd.held or sha in rnd.outdated


def _by_source(pairs):
    out = {}
    for src, sha in sorted(set(pairs)):
        out.setdefault(src, []).append(sha)
    return out


def settle(sel, spend, deadline):
    """(failures, stopped): every complete candidate of the run tags (a recheck drawn differently:
    stone_pipeline.photos.source.draw_candidate) judged through verdict, as a new result is, each started before the
    deadline; then each settled (settle_one). A promotion or removal that fails stops the run loud, before report and
    publish: the next run finishes it. A photo of a listing left exactly as published (sel.frozen) is skipped, its
    candidate kept: it settles normally once that listing's choice record can be read again. The rounds are read again
    after."""
    found = [{"key": f"{tag}/{src}/{sha}", "cands": rnd.redraw, "source": src, "sha": sha}
             for tag, by_source in sel.rounds for src, rnd in sorted(by_source.items()) for sha in sorted(rnd.redrawn)
             if (src, sha) not in sel.frozen]
    judged = lambda c: verdict(sel.client, c["cands"], c["source"], c["sha"], C.CUTOUT_FORMAT, spend)
    failures, stopped = _each([c for c in found if undecided(sel.client, c["cands"].out, c["sha"])], judged, deadline)
    for c in found:
        settle_one(sel.client, c["cands"], c["source"], c["sha"])
    sel.refresh()
    return failures, stopped


def finish(client, tags):
    """At the start of every run, before anything else (a close-only run settles nothing, a run can stop before its
    settle): every complete candidate of the run tags settled by its stored verdict (settle_one), with no paid call,
    so a promotion cut short is never published half done. The number settled or removed."""
    done = 0
    for tag in tags:
        root = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/"
        pages = client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=root, Delimiter="/")
        for out in [p["Prefix"] for page in pages for p in page.get("CommonPrefixes", [])]:
            cands = read_candidates(client, out)
            done += sum(settle_one(client, cands, out[len(root):-1], sha) for sha in sorted(cands.complete))
    return done


def settle_one(client, cands, src, sha):
    """One complete candidate (of `cands`) settled under the rule its marker names, by its stored verdict, never a paid
    call: one already noted under that rule (its removal cut short) removed; a pass promoted over its result; a fail, or
    a candidate of an earlier rule never judged, discarded (the result kept as it is); a candidate of the current rule
    with no current verdict yet (the budget, the window) left for settle to judge. Noted under its own rule, a candidate
    of an earlier rule leaves the photo due under the current one, checked whole. Whether it was settled or removed. The
    owner's choice and the judge's are settled alike: the choice is the photo, whichever result it shows."""
    out, rule = cands.out, candidate_rule(client, cands.out, sha)
    if noted(client, out, sha, rule):
        remove_candidate(client, out, sha)
        return True
    found = verdict(client, cands, src, sha, C.CUTOUT_FORMAT)
    if rule == C.OUTLINE_CHECKED and (found is None or not judge.current(found)):
        return False
    if found is not None and judge.passed(found):
        promote(client, out, sha, rule)
    elif found is None:
        discard(client, out, sha, f"drawn under {rule}, never judged: kept as it was", rule)
    else:
        kinds = ", ".join(p.get("kind", "?") for p in found.get("problems", [])) or found.get("verdict", "")
        discard(client, out, sha, f"the judge failed its redraw ({kinds}): kept as it was", rule)
    return True


def judge_all(sel, spend, deadline):
    """(failures, stopped): every listing judged until its first pass, each started before the deadline."""
    return _each(sel.listings, lambda l: choose(sel.client, sel.rounds, l, sel.blocked(l), spend, sel.owner_pick(l)), deadline)


def clean_all(sel, plans, spend, deadline):
    """(failures, stopped): each admitted plan paid (pay), each started before the deadline; admission
    (stone_pipeline.photos.process.admit) has already held their worst case within both budgets, so which listings are paid
    never depends on which thread reaches the cap first."""
    by_key = {plan.listing["key"]: plan for plan in plans}
    return _each([plan.listing for plan in plans], lambda l: pay(sel.client, sel.rounds, by_key[l["key"]], spend), deadline)


def report(sel, waits, marks, merge=False):
    """{"chosen", "tracked", "pending", "click_problems", "review"} of this run's listings; `waits` = {listing key: why
    its click's new edit waits} and `marks` = {listing key: why its other new edits wait} from the run's admission
    (stone_pipeline.photos.process.admit), so a click or a mark that waits says so on the review list, and each blocked click
    is a problem line (click_state). review = {SKU: review_row} of each listing without a good photo (tracked or
    pending), for review.json. merge (a clicks-only run, over a few listings): tracked.csv keeps every other listing's
    row exactly as it is and only this run's listings' rows are replaced (merged_tracked); else it is this run's list. Each listing with a good photo gets its choice record (a
    kept close-up's stays as it is unless a whole slab passed), a listing now without one loses its record (and is in
    tracked.csv, this run's list of listings with no good photo); a listing not fully judged is neither and its record
    is left as it is. Listings outside this run are untouched."""
    prefix = chosen_prefix(sel.root)

    def one(l):
        """(outcome, what goes in its list, its click's state, its review row or None) of one listing, from its stored
        answers only."""
        click, state = sel.click_state(l)
        if l["key"] in waits and state == ("ready", None):
            state = ("waiting", waits[l["key"]])
        picked, complete = choose(sel.client, sel.rounds, l, sel.blocked(l), owner=sel.owner_pick(l))
        if picked:
            tag, sha = picked
            record = {"source": l["source"], "key": l["key"], "sku": l["sku"], "sha": sha, "tag": tag,
                      "prefix": round_prefix(tag, l["source"])}
            if picked != l.get("kept"):                # a kept choice is its record already: never written again
                _put_json(sel.client, f"{prefix}{l['key']}.json", record)
            return "chosen", record, state, None
        row = review_row(sel.client, sel.rounds, l, "tracked" if complete else "pending", sel.exclude, sel.blocked(l),
                         click, state, marks.get(l["key"]))
        if complete:
            sel.client.delete_object(Bucket=S3_BUCKET, Key=f"{prefix}{l['key']}.json")
            return "tracked", {"source": l["source"], "listing": l["key"], "format": l["format"], "photos": len(l["photos"]),
                               "what_happened": photo_notes(sel.client, sel.rounds, l, sel.exclude, sel.conflicts,
                                                            click, state)}, state, row
        return "pending", l["key"], state, row
    outcomes = pool_map(C.SELECT_WORKERS, one, sel.listings)   # a few S3 reads per listing, in the listings' order
    chosen, tracked, pending = ([item for kind, item, _, _ in outcomes if kind == want] for want in ("chosen", "tracked", "pending"))
    clicks = [f"{l['key']}: {click_line(sel.clicks[l['sku']], state)}" for l, (_, _, state, _) in zip(sel.listings, outcomes)
              if state is not None and state[0] == "blocked"]
    review = {l["sku"]: row for l, (_, _, _, row) in zip(sel.listings, outcomes) if row is not None}
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=TRACKED_FIELDS)
    writer.writeheader()
    writer.writerows(merged_tracked(sel, tracked) if merge else tracked)
    _put(sel.client, f"{sel.root}tracked.csv", out.getvalue().encode(), "text/csv")
    return {"chosen": chosen, "tracked": tracked, "pending": pending, "click_problems": clicks, "review": review}


TRACKED_FIELDS = ["source", "listing", "format", "photos", "what_happened"]


def merged_tracked(sel, tracked):
    """tracked.csv's rows with this selection's listings' rows replaced by `tracked` (manager ruling C1, 2026-11-07: a
    clicks-only run never rewrites the list from its few listings): every other row read back as stored, so it is
    written again byte for byte; this run's rows after them. No list yet: this run's rows."""
    try:
        body = sel.client.get_object(Bucket=S3_BUCKET, Key=f"{sel.root}tracked.csv")["Body"].read().decode()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in MISSING:
            raise
        return tracked
    own = {l["key"] for l in sel.listings}
    return [row for row in csv.DictReader(io.StringIO(body)) if row["listing"] not in own] + tracked


def _each(listings, work, deadline, clock=time.monotonic):
    """work(listing) on every listing, SELECT_WORKERS at a time: (failures, stopped). A failure is reported and the rest go on;
    reaching a cap (or a stop) stops what has not started. stopped: the run's paid work has ended (the process budget,
    a stop), so no step after this one starts; the cleaning cap ends only this step (CleanCapReached). A listing not
    started by the deadline (the process's work window, on `clock`) is left as it is: the report counts it pending and
    the next run goes on with it."""
    stop, ended = threading.Event(), threading.Event()
    failures = []

    def one(listing):
        if stop.is_set() or clock() >= deadline:
            return
        try:
            work(listing)
        except CleanCapReached as exc:
            stop.set()
            failures.append((listing["key"], str(exc)))
        except (SpendCapReached, Halted) as exc:
            stop.set()
            ended.set()
            failures.append((listing["key"], str(exc)))
        except Exception as exc:                       # one listing never stops the others; the re-run retries it
            failures.append((listing["key"], f"{type(exc).__name__}: {exc}"))
    pool = ThreadPoolExecutor(C.SELECT_WORKERS)
    try:
        list(pool.map(one, listings))
    except BaseException:                              # stopped (a signal): nothing that has not started starts; running
        stop.set()                                     # listings cannot pay any more (Spend.halt); the lock and the GPU
        pool.shutdown(wait=False, cancel_futures=True)  # ceiling are released now, the process ends once they finish
        raise
    pool.shutdown(wait=True)
    return failures, ended.is_set()
