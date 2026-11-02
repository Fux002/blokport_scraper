"""Choose each listing's one good photo (user 2026-11-01: one very good image per listing is enough; it gives every view).
The steps deploy.standardise_run calls in order; hosted calls only, no model runs here. Two run tags take part: the
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

import csv
import io
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from deploy.standardise_publish import EXCLUDE_PATH, chosen_prefix, pool_map, read_exclude
from deploy.standardise_source import round_prefix
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import clean, judge

_SHA = re.compile(r"[0-9a-f]{64}")


class SpendCapReached(RuntimeError):
    pass


class Halted(RuntimeError):
    """The run was stopped: no hosted call may start any more."""


class Spend:
    """Spend against the month's budget (hosted calls and GPU rounds): `before` is what the month's other runs spent
    (others(), summed again at every settle, so a run written since is never missed), then this run's. A price is
    reserved before the call or round and settled to what it cost, so parallel calls can never together pass the
    budget. save(total) keeps this run's total after every change, a reserve counting as spent until it is settled, so
    a run that dies leaves an upper bound of what it spent, never too little. Storage is never asked while the spend's
    lock is held (a slow write never blocks the other calls), and a stop halts it without taking that lock (a SIGTERM
    may arrive while it is held)."""

    def __init__(self, budget, others=None, save=None):
        self.budget, self.others, self.save = budget, others, save
        self.before = others() if others else 0.0
        self.spent, self.reserved, self.capped = 0.0, 0.0, False
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
                self.capped = True
                raise SpendCapReached(f"spend would pass the month's budget of ${self.budget:.2f} "
                                      f"(spent ${self.before + self.spent:.2f})")
            self.reserved += usd
            self._version += 1
        self._keep()

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

    def _keep(self):
        """Store this run's total, outside the spend's lock: one write at a time, and only a total newer than the one
        stored, so overlapping calls can never leave an older total last."""
        if self.save is None:
            return
        with self._io:
            with self._lock:
                total, version = self.spent + self.reserved, self._version
            if version > self._saved:
                self.save(total)
                self._saved = version


class Round:
    """What one run tag holds for one source, its keys listed once: finished photos, the whole slabs among them, held
    ones, verdicts, cleaned copies and clean attempts ("<sha>.<model>": its check, its edited copy, its stored hosted
    request). One lock per photo: a photo several listings share is judged and cleaned once, never by two threads at
    once."""

    def __init__(self, client, tag, src):
        self.prefix = round_prefix(tag, src)
        keys = [o["Key"][len(self.prefix):] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=self.prefix)
                for o in page.get("Contents", [])]
        self.finished = {k[:64] for k in keys if k.endswith(".main.jpg") and "/" not in k}
        self.slabs = {k[:64] for k in keys if k.endswith(".slab.png") and "/" not in k}     # whole slabs: a 3D slab each
        self.studio = {k[:64] for k in keys if k.endswith(".studio.jpg") and "/" not in k}
        self.noslab = _shas(keys, "noslab/")
        self.held = _shas(keys, "held/")
        self.judged = _shas(keys, "judged/")
        self.cleaned = _shas(keys, "cleaned/")
        self.requested = _attempts(keys, ".request.json")
        self.edited = _attempts(keys, ".jpg")
        self.attempts = _attempts([k for k in keys if not k.endswith(".request.json")], ".json")
        self._locks, self._guard = {}, threading.Lock()

    def outdated(self):
        """The results made before an open book's halves had their slab (a side view, no slab, not tried again): the GPU
        completes each once (deploy.standardise_source.complete)."""
        return (self.finished & self.studio) - self.slabs - self.noslab - self.held

    def lock(self, sha):
        with self._guard:
            return self._locks.setdefault(sha, threading.Lock())


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
        if sha in rnd.judged:
            stored = _get_json(client, key)
            if judge.current(stored) or spend is None:     # a fail under an older, stricter bar is judged again
                return stored
        elif spend is None:
            return None
        original, result = _original(client, src, sha), _get(client, f"{rnd.prefix}{sha}.main.jpg")   # read before paying
        found, cost = paid(spend, C.JUDGE_PRICE_USD, lambda: judge.judge(original, result, fmt), f"judge {src} {sha[:12]}")
        _put_json(client, key, {**found, "cost": cost})
        rnd.judged.add(sha)
        return found


def paid(spend, price, call, what):
    """(answer, cost) of one hosted call returning (answer, cost): its price reserved first, then settled to what it
    cost. A call that fails may have been billed (sent, then lost), and an answer without its cost has one: both count at
    the reserved price, never as free."""
    spend.reserve(price)
    try:
        answer, cost = call()
    except BaseException:
        spend.settle(price, price)
        raise
    if cost is None:
        print(f"   WARNING: {what}: the answer carries no usage.cost; counted at the reserved ${price:.2f}", flush=True)
        cost = price
    spend.settle(price, cost)
    return answer, cost

def candidates(listing, exclude):
    """The photos a listing is judged on: its first JUDGE_MAX_PER_LISTING photos, excluded ones left out first. The GPU
    works exactly these (Selection.gpu_todo) and the judge looks at exactly these, so the two never differ."""
    return [sha for sha in listing["photos"] if sha not in exclude][:C.JUDGE_MAX_PER_LISTING]


def choose(client, rounds, listing, exclude, spend=None):
    """((tag, sha) of the listing's photo to publish or None, whether the answer is final): the standardiser's results of
    its candidates in order, then its cleaned copies' results, the first the judge passes. A slab listing prefers a whole
    slab (its result has a 3D slab): a passing close-up is the answer only while no whole slab passes, and once one is had
    (listing["kept"]: the choice a run made before, or the first close-up passed here) only whole results are judged.
    A photo held by the standardiser is skipped; a photo with no result yet, or no verdict yet, leaves the answer open
    (never read as "no good photo", which would send the listing to the hosted edit or the tracked list). rounds =
    [(tag, {source: Round})], the scraped run first, then (optionally) the cleaned run."""
    src = listing["source"]
    whole_wanted = listing["format"] == C.CUTOUT_FORMAT
    fallback, complete = listing.get("kept"), True
    for i, (tag, rnd) in enumerate(rounds):
        r = rnd[src]
        shas = candidates(listing, exclude) if i == 0 else [sha for sha in listing["photos"] if sha in r.cleaned and sha not in exclude]
        for sha in shas:
            if sha in r.held:
                continue
            if sha not in r.finished:
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
    """Whether a cleaned copy of this photo could pass: the judge failed its result only for reflections, marks or fills,
    or the retouch or logo step held it. A photo held for its outline, or never judged, is not worth an edit."""
    if sha in exclude:
        return False
    if sha in rnd.finished:
        if sha not in rnd.judged:
            return False
        found = _get_json(client, f"{rnd.prefix}judged/{sha}.json")
        return judge.current(found) and judge.fixable(found)          # an outdated fail is judged again first
    if sha in rnd.held:
        return _get_json(client, f"{rnd.prefix}held/{sha}.json")["reason"].startswith(C.CLEAN_FIXES_HELD)
    return False


def clean_photo(client, cl, src, sha, spend):
    """Each model in turn until a cleaned copy passes the clean check; that copy becomes cleaned/<sha>.jpg. Returns
    whether one passed."""
    with cl.lock(sha):
        if sha in cl.cleaned:
            return True
        original = _original(client, src, sha)
        for name in clean.MODELS:
            if _attempt(client, cl, sha, name, original, spend)["passes"]:
                client.copy_object(Bucket=S3_BUCKET, Key=f"{cl.prefix}cleaned/{sha}.jpg", ContentType="image/jpeg",
                                   CopySource={"Bucket": S3_BUCKET, "Key": f"{cl.prefix}attempts/{sha}.{name}.jpg"},
                                   MetadataDirective="REPLACE")
                cl.cleaned.add(sha)
                return True
        return False


def _attempt(client, cl, sha, name, original, spend):
    """The clean check of model `name`'s copy of the photo, made once. A stored check is read back, a stored copy is
    checked, a stored request is collected; only a photo never sent is sent, its price counted as soon as the queue
    accepts it (it is paid even if collecting it fails later). A request the queue no longer has is forgotten, so the
    next run sends it again."""
    att, ident = f"{cl.prefix}attempts/{sha}.{name}", f"{sha}.{name}"
    if ident in cl.attempts:
        stored = _get_json(client, f"{att}.json")
        if stored.get("passes") or stored.get("clean_version", 1) >= C.CLEAN_VERSION:
            return stored
        cl.edited.discard(ident)                       # failed under an older instruction: made again with the current one
        cl.requested.discard(ident)
    if ident in cl.edited:
        copy = _get(client, f"{att}.jpg")
    else:
        if ident in cl.requested:
            job = _get_json(client, f"{att}.request.json")
        else:
            price = C.CLEAN_PRICE_USD[name]                # an edit is billed at its list price once the queue has it
            job, _ = paid(spend, price, lambda: (clean.submit(name, original), price), f"edit {name} {sha[:12]}")
            _put_json(client, f"{att}.request.json", job)
            cl.requested.add(ident)
        try:
            copy = clean.collect(job)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (404, 410):                     # the queue no longer has it: the next run sends it again
                client.delete_object(Bucket=S3_BUCKET, Key=f"{att}.request.json")
                cl.requested.discard(ident)
                raise
            if code in C.CLEAN_FAILED_CODES:           # the model refused this photo for good: the next model
                return _failed(client, cl, att, ident, name, f"the edit failed ({code})")
            raise                                      # anything else (auth, balance, 5xx, 429): loud, collected again
        except clean.NoImage as exc:
            return _failed(client, cl, att, ident, name, str(exc))
        _put(client, f"{att}.jpg", copy, "image/jpeg")
        cl.edited.add(ident)
    check, cost = paid(spend, C.JUDGE_PRICE_USD, lambda: judge.check_clean(original, copy), f"clean check {name} {sha[:12]}")
    _put_json(client, f"{att}.json", {**check, "edit_model": name, "check_cost": cost, "clean_version": C.CLEAN_VERSION})
    cl.attempts.add(ident)
    return check

def _failed(client, cl, att, ident, name, why):
    """A hosted edit that failed for good, recorded as a failed attempt: the next model gets its turn, now and on re-runs."""
    check = {"clean": False, "faithful": False, "passes": False, "problems": why, "edit_model": name, "check_cost": 0.0,
             "clean_version": C.CLEAN_VERSION}
    _put_json(client, f"{att}.json", check)
    cl.attempts.add(ident)
    return check


def clean_listing(client, rounds, listing, exclude, spend):
    """A listing with no good photo from the standardiser: its first CLEAN_PHOTOS_PER_LISTING fixable photos are
    cleaned (one is enough). Nothing to do when it already has a cleaned copy (its result is judged later)."""
    (_, gpu), (_, cl) = rounds
    src = listing["source"]
    if any(sha in cl[src].cleaned for sha in listing["photos"]):
        return
    picked, complete = choose(client, rounds[:1], listing, exclude)
    if picked is not None:
        return
    if not complete:
        raise RuntimeError("its first photos have no standardiser result or verdict yet: nothing is cleaned before they do")
    for sha in [sha for sha in listing["photos"] if fixable(client, gpu[src], sha, exclude)][:C.CLEAN_PHOTOS_PER_LISTING]:
        if clean_photo(client, cl[src], src, sha, spend):
            return

def photo_notes(client, rounds, listing, exclude):
    """What happened to each of a listing's photos, for a person reading the tracked list."""
    notes = []
    src = listing["source"]
    for sha in listing["photos"]:
        parts = []
        for tag, rnd in rounds:
            r = rnd[src]
            if sha in exclude:
                parts.append("excluded after review")
                break
            if sha in r.judged:
                v = _get_json(client, f"{r.prefix}judged/{sha}.json")
                kinds = ", ".join(sorted({p.get("kind", "?") for p in v.get("problems", [])}))
                parts.append(f"{tag}: judge {v['verdict']}" + (f" ({kinds})" if kinds else ""))
            elif sha in r.finished:
                parts.append(f"{tag}: finished, not judged")
            elif sha in r.held:
                parts.append(f"{tag}: held: {_get_json(client, f'{r.prefix}held/{sha}.json')['reason']}")
            tried = [a for a in sorted(r.attempts) if a.startswith(sha)]
            if tried:
                parts.append("cleaned by " + ", ".join(a.split(".", 1)[1] for a in tried) + ("; a copy passed" if sha in r.cleaned else "; none passed"))
        notes.append(f"{sha[:12]}: " + ("; ".join(parts) or "no result"))
    return " | ".join(notes)


class Selection:
    """The listings one run works on and what the two run tags hold for them (re-read after each GPU round)."""

    def __init__(self, client, listings, run_tag, clean_tag):
        if not run_tag or not clean_tag or run_tag == clean_tag:
            raise ValueError("two different run tags are required: the scraped photos' run and the cleaned copies' run")
        self.client, self.listings, self.tags = client, listings, (run_tag, clean_tag)
        self.sources = sorted({l["source"] for l in listings})
        self.exclude = read_exclude(EXCLUDE_PATH.read_text())
        self.root = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{clean_tag}/"
        self.refresh()

    def refresh(self):
        self.rounds = [(tag, {src: Round(self.client, tag, src) for src in self.sources}) for tag in self.tags]

    def gpu_todo(self):
        """{source: shas} of each listing's candidates (the photos the judge looks at) the standardiser has not run yet,
        or whose result is outdated."""
        gpu = self.rounds[0][1]
        return _by_source((l["source"], sha) for l in self.listings for sha in candidates(l, self.exclude)
                          if needs_gpu(gpu[l["source"]], sha))

    def clean_todo(self):
        """{source: shas} with a cleaned copy the standardiser has not run yet, or whose result is outdated."""
        cl = self.rounds[1][1]
        return _by_source((src, sha) for src in self.sources for sha in cl[src].cleaned if needs_gpu(cl[src], sha))


def needs_gpu(rnd, sha):
    """Whether the GPU works on this photo: no result yet, or an outdated one (Round.outdated)."""
    return sha not in rnd.finished | rnd.held or sha in rnd.outdated()


def _by_source(pairs):
    out = {}
    for src, sha in sorted(set(pairs)):
        out.setdefault(src, []).append(sha)
    return out


def judge_all(sel, spend, deadline):
    """(failures, stopped): every listing judged until its first pass, each started before the deadline."""
    return _each(sel.listings, lambda l: choose(sel.client, sel.rounds, l, sel.exclude, spend), deadline)


def clean_all(sel, spend, deadline):
    """(failures, stopped): every listing with no pass gets a cleaned copy where one can be made, each started before the
    deadline."""
    return _each(sel.listings, lambda l: clean_listing(sel.client, sel.rounds, l, sel.exclude, spend), deadline)


def report(sel):
    """{"chosen", "tracked", "pending"} of this run's listings. Each listing with a good photo gets its choice record (a
    kept close-up's stays as it is unless a whole slab passed), a listing now without one loses its record (and is in
    tracked.csv, this run's list of listings with no good photo); a listing not fully judged is neither and its record
    is left as it is. Listings outside this run are untouched."""
    prefix = chosen_prefix(sel.root)

    def one(l):
        """(outcome, what goes in its list) of one listing, from its stored answers only."""
        picked, complete = choose(sel.client, sel.rounds, l, sel.exclude)
        if picked:
            tag, sha = picked
            record = {"source": l["source"], "key": l["key"], "sha": sha, "tag": tag, "prefix": round_prefix(tag, l["source"])}
            if picked != l.get("kept"):                # a kept choice is its record already: never written again
                _put_json(sel.client, f"{prefix}{l['key']}.json", record)
            return "chosen", record
        if complete:
            sel.client.delete_object(Bucket=S3_BUCKET, Key=f"{prefix}{l['key']}.json")
            return "tracked", {"source": l["source"], "listing": l["key"], "format": l["format"], "photos": len(l["photos"]),
                               "what_happened": photo_notes(sel.client, sel.rounds, l, sel.exclude)}
        return "pending", l["key"]
    outcomes = pool_map(C.SELECT_WORKERS, one, sel.listings)   # a few S3 reads per listing, in the listings' order
    chosen, tracked, pending = ([item for kind, item in outcomes if kind == want] for want in ("chosen", "tracked", "pending"))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=["source", "listing", "format", "photos", "what_happened"])
    writer.writeheader()
    writer.writerows(tracked)
    _put(sel.client, f"{sel.root}tracked.csv", out.getvalue().encode(), "text/csv")
    return {"chosen": chosen, "tracked": tracked, "pending": pending}


def _each(listings, work, deadline, clock=time.monotonic):
    """work(listing) on every listing, WORKERS at a time: (failures, stopped). A failure is reported and the rest go on;
    reaching the budget (or a stop) stops what has not started. A listing not started by the deadline (the process's
    work window, on `clock`) is left as it is: the report counts it pending and the next run goes on with it."""
    stop = threading.Event()
    failures = []

    def one(listing):
        if stop.is_set() or clock() >= deadline:
            return
        try:
            work(listing)
        except (SpendCapReached, Halted) as exc:
            stop.set()
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
    return failures, stop.is_set()
