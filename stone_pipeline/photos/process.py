"""The standardised-photo process of an environment (STANDARDISE_PROCESS.md): every listing of the product sheet without
a valid chosen photo ends with one good photo, published and linked, or on the review list with the reason. The
runner's `photos` stage, started after each full produce on the owner's button. In order:
  0. clear: GPU jobs an earlier, killed run left are stopped and the GPU pool's ceiling is set back to idle
  1. pick: the sheet's listings without a valid choice record (missing, its photo no longer the listing's or excluded,
     not the owner's choice, or a photo listings of several stones show that the owner did not assign), never one the
     owner removed; and the slab listings whose chosen photo is no whole slab (a close-up: no 3D slab), their choice kept;
     only of the sources whose image pipeline is on. Of those, the listings with the owner's click go first: each ready
     click both budgets can finish (run.admit) is cleaned, run through the GPU and judged (steps 4 to 6 for those
     listings alone) before step 2; a click they cannot finish is not paid and waits, named in the run's problems.
     Step 4 then admits the clicks that became ready this run first, then the marks, by the same rule
  2. GPU: their first photos the standardiser has not run yet (stone_pipeline.photos.batch), under STANDARDISE.RUN_TAG,
     and every chosen result outdated (a whole slab drawn under an older outline rule: live_choices), its listing not
     picked again; then settle: a recheck drawn differently is a candidate beside its result, judged as a new result
     is, promoted over it on a pass or discarded on a fail (select.settle)
  3. judge: the owner's choice of a listing, else its results in its own order until one passes
  4. clean: a hosted edit of one fixable photo of each listing still without a pass
  5. GPU: the cleaned copies, through every step a scraped photo takes, under STANDARDISE.CLEAN_TAG
  6. judge: the cleaned copies' results
  7. report: one choice record per listing, tracked.csv (the review list) and summary.json, under the clean tag
  8. publish: the published folder made to match the choice records (stone_pipeline.photos.publish), then review.json (the
     hidden-listings page's list: the report's rows and the tiles publish could not render; a close-only run leaves it)
  9. relink: when the folder changed, produce --stage republish, so the new photos are linked in this same run
With SCRAPER_STANDARDISE_CLOSE_ONLY on (the runner sets it for a close_only photos run), steps 1 to 7 are one close-up
round instead: every chosen photo's close-up not made yet under the current rule made again from the photo its run
started from (stone_pipeline.photos.close, a GPU job per slice, under the same lock, budget, window and run id), then
publish and relink as above; its summary is close-summary.json.
With SCRAPER_STANDARDISE_SCOPE_KEY set (a scoped run: read_scope), steps 1 to 7 work the scope's listings alone, in its
order, and its listed finished photos go to the GPU once more under STANDARDISE.RESTANDARDISED (a candidate the judge
settles, never over the published result; settled, the rule's note is the record, so a re-run never redoes it; one
kept as it was is listed in the summary, restandardise_kept). The decisions, conflict assignments and publish stay the
whole sheet's, and its rows merge into tracked.csv and review.json, so no other listing is touched. It needs
SCRAPER_STANDARDISE_RUN_CAP_USD: what the run may spend in all, binding both budgets whatever the months leave.
Every step stores what it did, so a run that stops is run again and goes on where it stopped, paying nothing twice. The
GPU and hosted steps work at most STANDARDISE.WORK_SECONDS: after it no hosted call and no GPU round starts. The judge
and GPU rounds together never take the month past STANDARDISE.BUDGET_USD, and the clean-up edits with their checks never
past STANDARDISE.CLEAN_BUDGET_USD (each run's spend of the month is its own file under <env>/standardise-test/spend/ and
clean-spend/<YYYY-MM>/, the latter listing every paid call).
One process at a time per environment: it holds <env>/standardise-test/run.lock (written only if absent, rewritten every
LOCK_HEARTBEAT_SECONDS while it runs, taken over only when not rewritten for the runner's ceiling: its run was killed)
and removes it, if it is still its own, when publish has ended. SIGTERM (the service stopping) unwinds the same way: the
round's jobs are stopped, the ceiling set back, the spend kept, the lock released.
Env: SCRAPER_GPU_QUEUE, SCRAPER_STANDARDISE_JOBDEF, FAL_KEY (not for a close-only run), SCRAPER_STANDARDISE_BUDGET_USD
(all required); SCRAPER_STANDARDISE_CLEAN_BUDGET_USD (unset: no edit is paid); SCRAPER_RUN_SUMMARY (set by the runner): where the run's summary is handed back for its run record;
SCRAPER_STANDARDISE_CLOSE_ONLY (set by the runner, true only for a close_only run): the close-up round instead of 1 to 7;
SCRAPER_STANDARDISE_CLICKS_ONLY (set by the runner, true only for a clicks_only run): steps 1 to 7 for the listings with
the owner's click alone (the clicks' steps), their rows merged into tracked.csv and review.json, summary
clicks-summary.json. SCRAPER_STANDARDISE_SCOPE_KEY (a scoped run's scope) with SCRAPER_STANDARDISE_RUN_CAP_USD.
Exit 0 the run went through (pending listings, problems and a reached budget are in its summary); 2 not configured,
no produce has stored the conflict set yet, or a scope that cannot be worked (ScopeRefused);
4 another process is running; a failed relink exits with the produce's code; any other failure is loud (a traceback).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from botocore.exceptions import BotoCoreError, ClientError

from stone_pipeline.photos import batch as gpu
from stone_pipeline.photos import close as close
from stone_pipeline.photos import publish as publish
from stone_pipeline.photos import select as select
from stone_pipeline.photos.source import checked_key, round_prefix, sheet_and_photos
from stone_pipeline.config.settings import ENV_SEGMENT, RUN_TIMEOUT_SECONDS, S3_BUCKET
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.config.sources import image_pipeline_off
from stone_pipeline.core import env
from stone_pipeline.io import imagestore
from stone_pipeline.io.storage import PROCESS_RETRIES, aws_client
from stone_pipeline.ledger import writethrough
from stone_pipeline.standardise import index, judge

ROOT = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/"
LOCK = f"{ROOT}run.lock"
_TAKEN = ("PreconditionFailed", "ConditionalRequestConflict")


class RunActive(RuntimeError):
    pass


def _code(exc):
    return exc.response.get("Error", {}).get("Code")


# --- the run lock -----------------------------------------------------------------------------------------------------
def _lost(s3, etag, exc):
    """Whether a failed conditional write means the lock is no longer this run's. A PreconditionFailed (replaced) and a
    404 (gone) say so; a 409 (ConditionalRequestConflict: another write was in flight) is checked against the lock's
    ETag; any other failure is a passing one."""
    if not isinstance(exc, ClientError):
        return False
    if _code(exc) == "PreconditionFailed" or _code(exc) in publish.MISSING:
        return True
    if _code(exc) != "ConditionalRequestConflict":
        return False
    try:
        return s3.head_object(Bucket=S3_BUCKET, Key=LOCK)["ETag"] != etag
    except ClientError as missing:
        return _code(missing) in publish.MISSING
    except BotoCoreError:
        return False


@contextmanager
def exclusive(s3, owner, on_lost=None, now=None, beat=C.LOCK_HEARTBEAT_SECONDS):
    """Hold the environment's run lock for the block, rewriting it every `beat` seconds (its sign of life); RunActive
    when another process holds it. A lock taken over meanwhile calls on_lost and is never removed by this run."""
    held = {"etag": _acquire(s3, owner, now)}
    done = threading.Event()

    def heartbeat():
        while not done.wait(beat):
            try:
                held["etag"] = s3.put_object(Bucket=S3_BUCKET, Key=LOCK, Body=json.dumps(owner).encode(),
                                             IfMatch=held["etag"])["ETag"]
            except (BotoCoreError, ClientError) as exc:
                if not _lost(s3, held["etag"], exc):   # a passing error: the next beat tries again
                    print(f"   WARNING: the run lock was not refreshed: {exc}", flush=True)
                    continue
                print("==> the run lock was taken over by another process: no paid call starts any more", flush=True)
                held["etag"] = None
                if on_lost:
                    on_lost()
                return
    beating = threading.Thread(target=heartbeat, daemon=True)
    beating.start()
    try:
        yield
    finally:
        done.set()
        beating.join()
        _release(s3, held["etag"])


def _acquire(s3, owner, now):
    """The lock's ETag once this process holds it. Held by another: taken over only when not rewritten for the runner's
    ceiling (RUN_TIMEOUT_SECONDS; the runner ends every run before it, so its run was killed), in one write conditional on
    the lock seen, so of two processes taking it over one does. A lock released or rewritten while we looked (a 404, a
    409) is tried once more."""
    body = json.dumps(owner).encode()
    for _ in range(2):
        try:
            return s3.put_object(Bucket=S3_BUCKET, Key=LOCK, Body=body, IfNoneMatch="*")["ETag"]
        except ClientError as exc:
            if _code(exc) not in _TAKEN:
                raise
        try:
            held = s3.head_object(Bucket=S3_BUCKET, Key=LOCK)
        except ClientError as exc:
            if _code(exc) in publish.MISSING:
                continue
            raise
        since = held["LastModified"]
        if ((now or datetime.now(timezone.utc)) - since).total_seconds() < RUN_TIMEOUT_SECONDS:
            raise RunActive(f"another standardise process holds s3://{S3_BUCKET}/{LOCK} (alive at {since:%Y-%m-%d %H:%M} UTC)")
        try:
            etag = s3.put_object(Bucket=S3_BUCKET, Key=LOCK, Body=body, IfMatch=held["ETag"])["ETag"]
        except ClientError as exc:
            if _code(exc) == "PreconditionFailed":
                raise RunActive(f"another standardise process took over s3://{S3_BUCKET}/{LOCK} first") from exc
            if _code(exc) in _TAKEN:                   # a write was in flight: look again
                continue
            raise
        print(f"==> took over the lock of a killed run (last alive {since:%Y-%m-%d %H:%M} UTC)", flush=True)
        return etag
    raise RunActive(f"s3://{S3_BUCKET}/{LOCK} changed hands while starting; run again")


def lock_holder(s3):
    """The run id the run lock names (a photos run's, or a decisions apply's "decisions-..."), None when no lock is held:
    who to wait for, for an answer that cannot be given while a run holds it."""
    try:
        return json.loads(s3.get_object(Bucket=S3_BUCKET, Key=LOCK)["Body"].read()).get("run_id")
    except ClientError as exc:
        if _code(exc) in publish.MISSING:
            return None
        raise


def _release(s3, etag):
    """Remove the lock only while it is still this run's (a delete conditional on its ETag); a 409 while it is still
    ours is tried once more."""
    if etag is None:
        return
    for _ in range(2):
        try:
            s3.delete_object(Bucket=S3_BUCKET, Key=LOCK, IfMatch=etag)
            return
        except ClientError as exc:
            if _code(exc) not in _TAKEN + publish.MISSING:
                raise
            if _lost(s3, etag, exc):
                print("==> the run lock is no longer this run's: left as it is", flush=True)
                return
    print("==> the run lock could not be removed (writes in flight): it is taken over once stale", flush=True)


# --- the month's budget -----------------------------------------------------------------------------------------------
def spend_prefix(directory, month):
    return f"{ROOT}{directory}/{month}/"


def month_spend(s3, run_id, budget, directory, cap_error=select.SpendCapReached, run=None):
    """This run's Spend against one monthly budget whose runs keep their spend under `directory`. Each run keeps only its
    own file (<run id>.json, {"usd"}), so no run can overwrite another's; the month's other runs are summed from their
    files, read again only when changed (ETag). The month is the run's start (UTC), from its id (YYYYMMDDTHHMMSSZ)."""
    prefix = spend_prefix(directory, f"{run_id[:4]}-{run_id[4:6]}")
    own, seen = f"{prefix}{run_id}.json", {}

    def others():
        total = 0.0
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            for o in page.get("Contents", []):
                if o["Key"] == own:
                    continue
                if seen.get(o["Key"], (None,))[0] != o["ETag"]:
                    usd = json.loads(s3.get_object(Bucket=S3_BUCKET, Key=o["Key"])["Body"].read())["usd"]
                    seen[o["Key"]] = (o["ETag"], usd)
                total += seen[o["Key"]][1]
        return total

    def save(total, uses):
        s3.put_object(Bucket=S3_BUCKET, Key=own, Body=json.dumps({"usd": round(total, 6), "uses": uses}).encode(),
                      ContentType="application/json")
    return select.Spend(budget, others, save, cap_error, run)


# --- what a run works on ----------------------------------------------------------------------------------------------
def choices(s3, root):
    """({listing key: its choice record}, {listing key: why it cannot be read}) under the clean tag's root
    (publish.read_choices)."""
    return publish.read_choices(s3, root)


def whole_slabs(s3, records):
    """The <sha>.slab.png keys in the run folders the choice records point at: which chosen photos are a whole slab."""
    out = set()
    for prefix in sorted({r["prefix"] for r in records.values()}):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            out.update(o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".slab.png"))
    return out


def photo_listings(sheet, photos):
    """The sheet's listings shown from their own photos (index.listings): every one but a tile listing, which is
    rendered from its variety's slab stone at publish (stone_pipeline.photos.publish), never through the GPU and judge."""
    return [l for l in index.listings(sheet, photos) if l["format"] != C.TILE_FORMAT]


class ScopeRefused(ValueError):
    """A scoped run's scope file cannot be worked: not a scope, or it names a listing or photo the run cannot work. The
    run is refused before anything is paid, every offending name given (a typo must never shrink the run silently)."""


@dataclass(frozen=True)
class Scope:
    """A scoped run's scope (SCRAPER_STANDARDISE_SCOPE_KEY, one S3 JSON object read as ONLY_KEY's lists are): the SKUs
    of the listings to work, in that order, and the photos (shas) to re-standardise once (C.RESTANDARDISED)."""
    skus: tuple
    restandardise: frozenset


_SHA_TEXT = re.compile(r"[0-9a-f]{64}")


def read_scope(s3, key):
    """The Scope stored at `key`: {"listings": [SKU, ...], "restandardise": [sha, ...]}, both lists required (either may
    be empty but not both), a SKU listed once. ScopeRefused naming the key on anything else; a read failure raises."""
    try:
        doc = json.loads(s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
    except ValueError as exc:
        raise ScopeRefused(f"the scope {key} is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != {"listings", "restandardise"}:
        raise ScopeRefused(f"the scope {key} is not an object of exactly \"listings\" and \"restandardise\"")
    skus, shas = doc["listings"], doc["restandardise"]
    if not isinstance(skus, list) or not all(isinstance(v, str) and v.strip() == v and v for v in skus):
        raise ScopeRefused(f"the scope {key}'s listings are not a list of SKUs")
    if not isinstance(shas, list) or not all(isinstance(v, str) and _SHA_TEXT.fullmatch(v) for v in shas):
        raise ScopeRefused(f"the scope {key}'s restandardise is not a list of photo shas")
    if twice := sorted({v for v in skus if skus.count(v) > 1}):
        raise ScopeRefused(f"the scope {key} lists a SKU twice: {', '.join(twice)}")
    if not skus and not shas:
        raise ScopeRefused(f"the scope {key} names nothing to work")
    return Scope(tuple(skus), frozenset(shas))


def scoped(listings, scope):
    """The listings a run works on: every one (no scope, today's run), or the scope's, in its order. ScopeRefused naming
    every SKU the sheet does not have, and every SKU with its format whose listing gets no 3D cut-out (only a
    C.CUTOUT_FORMAT listing does; a block is paid for and gets none, and blocks get no 3D)."""
    if scope is None:
        return listings
    by_sku = {}
    for l in listings:
        by_sku.setdefault(l["sku"], []).append(l)
    if unknown := [sku for sku in scope.skus if sku not in by_sku]:
        raise ScopeRefused(f"the scope names listings the sheet does not have: {', '.join(unknown)}")
    work = [l for sku in scope.skus for l in by_sku[sku]]
    if no_cutout := list(dict.fromkeys(f"{l['sku']} ({l['format']})" for l in work if l["format"] != C.CUTOUT_FORMAT)):
        raise ScopeRefused(f"the scope names listings that get no 3D cut-out: {', '.join(no_cutout)}")
    return work


def restandardise_items(scope, listings, rounds):
    """{(source, sha)} of the scope's photos to re-standardise, each a finished photo (the run tag's round) of one of
    the scope's `listings`. ScopeRefused naming every one that is not."""
    finished = dict(rounds)[C.RUN_TAG]
    items, refused = set(), []
    for sha in sorted(scope.restandardise):
        owner = next((l for l in listings if sha in l["photos"]), None)
        if owner is None:
            refused.append(f"{sha[:12]}: no listed listing shows it")
        elif sha not in finished[owner["source"]].finished:
            refused.append(f"{sha[:12]}: not finished in {C.RUN_TAG} ({owner['source']})")
        else:
            items.add((owner["source"], sha))
    if refused:
        raise ScopeRefused("the scope's photos to re-standardise cannot all be: " + "; ".join(refused))
    return frozenset(items)


def scope_rounds(s3, scope, listings):
    """[(run tag, {source: Round})] of what the run tag holds of the scope's photos to re-standardise alone
    (select.Round of a few photos), per source of the scope's `listings`: what restandardise_items checks, read before
    the run's housekeeping so a refused scope does nothing."""
    by_source = {}
    for l in listings:
        by_source.setdefault(l["source"], set()).update(sha for sha in l["photos"] if sha in scope.restandardise)
    return [(C.RUN_TAG, {src: select.Round(s3, C.RUN_TAG, src, sorted(shas))
                         for src, shas in sorted(by_source.items())})]


def restandardise_kept(s3, items, run_folder):
    """[{source, sha, why}] of the re-standardised photos whose result was kept as it was (their note under
    C.RESTANDARDISED: the judge failed the redraw, or the re-run gave no whole slab), sorted: the 3D gate still blocks
    them, so the run lists them for the people, never leaves them silent. run_folder(source) = its run tag's folder. A
    photo not settled yet (no note) is not listed; a note that cannot be read raises."""
    def note(item):
        src, sha = item
        try:
            key = checked_key(run_folder(src), sha, C.RESTANDARDISED)
            body = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        except ClientError as exc:
            if _code(exc) in publish.MISSING:
                return None
            raise
        return json.loads(body)
    notes = publish.pool_map(C.STORE_WORKERS, note, sorted(items))
    return [{"source": src, "sha": sha, "why": n["kept"]} for (src, sha), n in zip(sorted(items), notes)
            if n is not None and "kept" in n]


def tile_listings(sheet, photos):
    """The sheet's tile listings (index.listings): publish renders them from their variety's slab stone, and the review
    list names those it could not render (tiles_hidden)."""
    return [l for l in index.listings(sheet, photos) if l["format"] == C.TILE_FORMAT]


def removed_listings(s3):
    """The SKUs of the listings the owner removed (stone_pipeline.photos.decisions)."""
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=imagestore.owner_removed_prefix())
            for o in page.get("Contents", [])]
    return imagestore.owner_removed_skus(keys)


def owner_records(s3, root):
    """({SKU: the owner's photo choice record}, {SKU: why its record could not be read}) under the clean tag's root
    (stone_pipeline.photos.decisions)."""
    return publish.records_by_sku(s3, publish.owner_prefix(root))


def valid_choice(l, r, exclude, conflicts, picks):
    """Whether a listing's choice record `r` is valid: its photo is one of the listing's and not excluded, and it is the
    owner's choice when the listing has one that applies (picks = {listing key: (tag, sha)}, select.owner_choice);
    without one it may not name a photo listings of several stones show (`conflicts`): that was chosen before it was
    one, and only the owner assigns it."""
    if r is None or r["sha"] not in l["photos"] or r["sha"] in exclude:
        return False
    if l["key"] in picks:
        return (r["tag"], r["sha"]) == picks[l["key"]]
    return (l["source"], r["sha"]) not in conflicts


def todo(listings, records, exclude, slabs, removed, conflicts, picks):
    """The listings a run works on, in sheet order, never one the owner removed (its SKU in `removed`). Those without a
    valid chosen photo (valid_choice). A listing on the review list has no record, so every run looks at it again (its stored
    answers cost nothing; a change that can help it reaches it). And a slab listing whose chosen photo is no whole slab
    (no <sha>.slab.png in `slabs`: a close-up, no 3D slab), with that choice as "kept": its further photos may show the
    whole slab, and the choice stays unless one of them passes; never one the owner chose a photo for (it stands). Its
    stored answers cost nothing either, so it is simply looked at again by every run. A listing of a source whose image
    pipeline is off is never picked: its products show the scraped photos, so nothing is spent on it."""
    off = image_pipeline_off()
    out = []
    for l in listings:
        if l["sku"] in removed:
            continue
        r = records.get(l["key"])
        if l["source"] in off:
            continue
        if not valid_choice(l, r, exclude, conflicts, picks):
            out.append(l)
        elif (l["key"] not in picks and l["format"] == C.CUTOUT_FORMAT
              and f"{r['prefix']}{r['sha']}.slab.png" not in slabs):
            out.append({**l, "kept": (r["tag"], r["sha"])})
    return out


def live_choices(listings, records, removed, decisions):
    """[(tag, source, sha)] of what the listings a run may publish show now: each one's owner choice that applies, else
    its valid choice record (valid_choice); never a listing the owner removed or of a source whose image pipeline is off
    (its products show the scraped photos). An outdated whole slab among them goes to the GPU without its listing being
    picked again (select.Selection). A listing that left the sheet keeps its record, but its photos are no longer listed
    for the GPU job, so it is not among them."""
    off = image_pipeline_off()
    out = set()
    for l in listings:
        if l["sku"] in removed or l["source"] in off:
            continue
        r = records.get(l["key"])
        if l["key"] in decisions.picks:
            out.add((decisions.picks[l["key"]][0], l["source"], decisions.picks[l["key"]][1]))
        elif valid_choice(l, r, decisions.exclude, decisions.conflicts, decisions.picks):
            out.add((r["tag"], l["source"], r["sha"]))
    return sorted(out)


def assignments(listings, picks, conflicts):
    """({(source, sha): SKUs} of every conflict photo, the problems): the one listing whose owner choice that applies names
    it (picks, select.owner_choice). A conflict photo the owner's choices name for more than one listing (the applier
    refuses that, so only an old or hand-made record) is linked to none, and said."""
    claims = {photo: [] for photo in conflicts}
    for l in listings:
        pick = picks.get(l["key"])
        if pick is not None and (l["source"], pick[1]) in claims:
            claims[(l["source"], pick[1])].append(l)
    problems = [f"photo {sha[:12]} ({C.CONFLICT_REASON}) is the owner's choice for "
                f"{', '.join(sorted(l['key'] for l in claimed))}: linked to none until one decision is withdrawn"
                for (_, sha), claimed in sorted(claims.items()) if len(claimed) > 1]
    return {photo: [claimed[0]["sku"]] if len(claimed) == 1 else [] for photo, claimed in claims.items()}, problems


# --- the steps --------------------------------------------------------------------------------------------------------
def halt(spends, why):
    """No paid call may start any more on any of the run's budgets (the process's and the cleaning cap's)."""
    for spend in spends:
        if spend is not None:
            spend.halt(why)


def stop_on_signal(*spends):
    """SIGTERM (the service stopping: the config server passes it on) unwinds like an error: no paid call may start any
    more, the round's jobs are stopped, and the GPU pool's ceiling, the spend and the run lock are settled by their
    finally blocks (which a bare SIGTERM would skip)."""
    def stopped(signum, frame):
        halt(spends, f"stopped by signal {signum}")
        raise SystemExit(f"stopped by signal {signum}")
    signal.signal(signal.SIGTERM, stopped)


def gpu_round(batch, sel, tag, todo, run_id, deadline, spend):
    """The GPU round for `todo`, then the stores re-read: (problems, stopped). Problems: failed jobs, a round stopped at
    the work window or stuck, a round Batch refused or failed (review 2026-11-06, A2.7: never the run's end), a round
    skipped (too little window left, or the budget left covers not one slice: the hosted steps still judge what is
    finished, within their own reservations), listed photos left without a result.
    stopped: paid work has ended (the window, a stop, a lost lock), so no step after it starts. A scoped run's round of
    the run tag also names its photos to re-standardise (sel.restandardise); any other round is called as it always was.
    """
    forced = sel.restandardise if tag == sel.tags[0] else frozenset()
    try:
        problems = [f"GPU job {name} failed: {why}"
                    for name, why in gpu.run_round(batch, sel.client, tag, todo, run_id, deadline, spend,
                                                   **({"forced": forced} if forced else {}))]
    except select.Halted as exc:
        return [f"GPU round {tag} not started: {exc}"], True
    except (gpu.Skipped, select.SpendCapReached) as exc:
        return [f"GPU round {tag} not started: {exc}"], False
    except TimeoutError as exc:                        # its jobs were stopped; what they finished stays
        problems = [f"GPU round {tag} stopped: {exc}"]
    except (BotoCoreError, ClientError) as exc:        # Batch refused or failed: its jobs were stopped and settled
        problems = [f"GPU round {tag} failed: {exc}"]
    sel.refresh()
    rnd = dict(sel.rounds)[tag]
    problems += [f"{src} {sha[:12]}: no result after the GPU round" for src, shas in todo.items() for sha in shas
                 if select.needs_gpu(rnd[src], sha)
                 or ((src, sha) in forced and select.restandardise_due(rnd[src], sha))]
    return problems, False


def hosted(step, sel, spend, deadline):
    """A hosted step over every listing, once: (problems, stopped). Every hosted step pays (a verdict, an edit, a check),
    so a listing that failed is tried again by the next run, from what is stored, never a second time in this one: a run
    never spends past what it admitted (review of #540)."""
    failures, stopped = step(sel, spend, deadline)
    return [f"{key}: {why}" for key, why in failures], stopped


WAIT_BUDGET = "month photo budget reached (GPU round and judge not covered); not paid"
WAIT_CAP = "cleaning cap reached (edits and checks not covered); not paid"
WAIT_WINDOW = "too little of the work window left for a GPU round; not paid"
NO_CAP = "no cleaning cap set"


def admit(sel, plans, left, cap_left, window):
    """(the plans the clean step pays, {listing key: why its new edits wait}) (review 2026-11-06, A2.3/A2.4: one
    admission, nothing paid that cannot finish, the same listings whatever the threads do). Read-only, in a fixed order.
    First every edit already paid for (a plan's collect): it is owed, so it always goes, its check and its copy counted.
    Then the new edits (a plan's send), the owner's clicks first, then the marks, each in sheet order: admitted while
    both budgets left cover them with everything counted before them: the process budget (`left`) the GPU round and
    judging of their copies (gpu.price, the same price a round is sized by), on top of what this selection's next GPU
    round runs anyway (clean_todo); the cleaning cap (`cap_left`) their edits and checks at worst (select.clean_price).
    With less than a GPU round's minimum of the window left no new edit is admitted. A plan whose send waits still
    collects what it owes. The pay step then never reaches either cap, so admission alone decides who is paid."""
    todo, waits, sending = sel.clean_todo(), {}, set()
    gpu_left, clean_left = left, cap_left

    def adding(todo, plan):
        """todo with the listing's one copy to come counted once (pay stops at the first copy that passes)."""
        src = plan.listing["source"]
        sha = (plan.collect or plan.send)[0][0]
        listed = todo.get(src, [])
        return todo if sha in listed else {**todo, src: listed + [sha]}
    for plan in plans:
        if plan.collect:
            todo, clean_left = adding(todo, plan), clean_left - select.clean_price(replace(plan, send=()))
    for plan in sorted((plan for plan in plans if plan.send), key=lambda plan: not plan.click):   # stable: sheet order
        trial, price = adding(todo, plan), select.clean_price(replace(plan, collect=()))
        if window < C.GPU_MIN_WINDOW_SECONDS:
            waits[plan.listing["key"]] = WAIT_WINDOW
        elif gpu.price(trial, window) > gpu_left:
            waits[plan.listing["key"]] = WAIT_BUDGET
        elif price > clean_left:
            waits[plan.listing["key"]] = WAIT_CAP
        else:
            todo, clean_left = trial, clean_left - price
            sending.add(plan.listing["key"])
    goes = [plan if plan.listing["key"] in sending else replace(plan, send=())
            for plan in plans if plan.collect or plan.listing["key"] in sending]
    return goes, waits


def clean_step(sel, plans, spend, clean, deadline, window):
    """The clean step over `plans`: admitted (admit), then paid (select.clean_all on the cleaning cap). (problems,
    stopped, the plans paid, the clicks that wait, the marks that wait): what could not be paid or read, and a line per
    send that waits (a click by its listing, the marks counted); {listing key: why} of the clicks whose own new edit
    waits and {listing key: why} of the other new edits that wait (the marks), each for the review list."""
    goes, waits = admit(sel, plans, spend.left(), clean.left(), window)
    clicks = {plan.listing["key"] for plan in plans if plan.sends_click}   # a wait of the click's own new edit
    problems = [f"{key}: the owner's click waits: {why}" for key, why in waits.items() if key in clicks]
    if marks := [why for key, why in waits.items() if key not in clicks]:
        problems += [f"the marks of {marks.count(why)} listings wait: {why}" for why in dict.fromkeys(marks)]
    found, stopped = hosted(lambda s, cap, d: select.clean_all(s, goes, cap, d), sel, clean, deadline)
    return (problems + found, stopped, goes, {key: why for key, why in waits.items() if key in clicks},
            {key: why for key, why in waits.items() if key not in clicks})


def owed_copy(sel, listing):
    """Whether the listing's clicked photo has a cleaned copy the GPU has not run yet (made by an earlier run): the
    clicks' step finishes it, as it was priced (admit counts clean_todo)."""
    rnd = sel.rounds[1][1][listing["source"]]
    sha = sel.clicks[listing["sku"]]["sha"]
    return sha in rnd.cleaned and select.needs_gpu(rnd, sha)


def through(steps, deadline, clock, problems):
    """Each step in turn inside the work window, its problems added to `problems`: whether paid work has ended (the
    window, or a step that ended it: the process budget, a stop)."""
    for step in steps:
        if clock() >= deadline:
            problems.append(f"the work window ({C.WORK_SECONDS}s) ended: the next run goes on")
            return True
        found, stopped = step()
        problems += found
        if stopped:
            return True
    return False


def run(batch, sel, spend, clean, run_id, deadline, say=print, clock=time.monotonic, clicks_only=False):
    """The GPU and hosted steps, then the report: (problems, report). The clean step (clean_step: admitted, then paid)
    comes twice. First the owner's clicks whose listing is ready (select.clean_plan: fully judged, no pass, a model left):
    cleaned, through the GPU and judged before any other work, the selection read again after. Then the run's own
    steps, its clean step over every listing, a click that only became ready this run (its outline settled in the
    run's GPU round) first. Nothing is paid that the budgets left cannot finish (admit); what waits is named. A step
    starts only inside the work window and while the budget lasts (the hosted clean-up edits and their checks on the
    cleaning cap, `clean`: None when it is not set, and then no photo is sent to an edit; the clean switch governs only
    the automatic edits, select.authorised); the GPU rounds end JUDGE_WINDOW_SECONDS before it, so the judge always has
    its share; after each of the run's GPU rounds the outline recheck's candidates are settled (select.settle); the
    report always runs, so what is chosen is recorded and published, unless a promotion failed: raised, and the next
    run of either kind finishes it at its start (select.finish, in process) before anything is published. clicks_only
    (a clicks-only run, its selection only the listings with a click): the clicks' steps alone, and its report merges
    into the review list (select.report merge); so does a scoped run's (sel.scoped: its listings are the scope's,
    every other listing's row stays)."""
    gpu_deadline = deadline - C.JUDGE_WINDOW_SECONDS
    problems = []
    # no cleaning cap: no edit can be paid, so every ready click and every mark says what it waits for
    waiting = {} if clean is not None else {l["key"]: NO_CAP for l in sel.listings if l["sku"] in sel.clicks}
    marks_waiting = {} if clean is not None else {l["key"]: NO_CAP for l in sel.listings}

    def cleaning(work):
        plans, failures = work.plans()
        if work is not sel:                            # the clicks' step: only the owner's clicks
            plans = [plan for plan in plans if plan.click]
        found, stopped, goes, waits, marks = clean_step(work, plans, spend, clean, deadline, gpu_deadline - clock())
        waiting.update(waits)
        marks_waiting.update(marks)
        return [f"{key}: {why}" for key, why in failures] + found, stopped, goes
    clicked = sel.only([l for l in sel.listings if l["sku"] in sel.clicks], sel.clicks)
    finishing = []                                     # the clicks' step finishes only what it admitted and owes

    def clicks_cleaned():
        found, stopped, goes = cleaning(clicked)
        paid = {plan.listing["key"] for plan in goes}
        finishing.append(clicked.only([l for l in clicked.listings if l["key"] in paid or owed_copy(clicked, l)],
                                      clicked.clicks))
        return found, stopped
    first = []
    if clean is not None and clicked.listings:
        say(f"==> the owner's clicks first: {len(clicked.listings)} listings with a click")
        first = [clicks_cleaned,
                 lambda: gpu_round(batch, finishing[0], finishing[0].tags[1], finishing[0].clean_todo(), run_id,
                                   gpu_deadline, spend),
                 lambda: hosted(select.judge_all, finishing[0], spend, deadline)]
    then = [lambda: gpu_round(batch, sel, sel.tags[0], sel.gpu_todo(), run_id, gpu_deadline, spend),
            lambda: hosted(select.settle, sel, spend, deadline),
            lambda: hosted(select.judge_all, sel, spend, deadline),
            *([lambda: cleaning(sel)[:2]] if clean is not None else []),
            lambda: gpu_round(batch, sel, sel.tags[1], sel.clean_todo(), run_id, gpu_deadline, spend),
            lambda: hosted(select.settle, sel, spend, deadline),
            lambda: hosted(select.judge_all, sel, spend, deadline)]
    ended = through(first, deadline, clock, problems)
    if first:
        sel.refresh()                                  # the clicks' steps stored new results and verdicts
    if not ended and not clicks_only:
        through(then, deadline, clock, problems)
    problems = list(dict.fromkeys(problems))           # a click waiting at both clean steps is named once
    result = select.report(sel, waiting, marks_waiting, merge=clicks_only or sel.scoped)
    by_tag = {tag: sum(c["tag"] == tag for c in result["chosen"]) for tag in sel.tags}
    say(f"==> {len(result['chosen'])} of {len(sel.listings)} listings with a good photo {by_tag}; "
        f"{len(result['tracked'])} on the review list (s3://{S3_BUCKET}/{sel.root}tracked.csv), {len(result['pending'])} pending")
    if result["pending"]:
        problems.append(f"{len(result['pending'])} listings not fully judged (listed in "
                        f"{sel.root}{summary_file(False, clicks_only)})")
    problems += result["click_problems"]                 # a click blocked (click_state), for any listing of the run
    return problems, result


@dataclass(frozen=True)
class Decisions:
    """What a run decides by, read once: the exclude list, the owner's choice records ({SKU: record}), the owner's clicks
    to clean a photo ({SKU: record}, publish.read_clicks), the listings whose choice record cannot be read ({SKU: why}),
    the produce's conflict set ({(source, sha): handles}: photos listings of several stones show), the run tags' stores,
    the owner's choices that apply ({listing key: (tag, sha)}, select.owner_choice) and the choice records ({listing key:
    record}, choices). A listing whose choice record cannot be read stays exactly as published: its current choice
    stands as its pick (so its conflict photo stays assigned and published), it is left out of the work, and every run
    names it. Both kinds of round and publish see the same."""
    exclude: set
    owner: dict
    clicks: dict
    conflicts: dict
    rounds: list
    picks: dict
    unreadable: dict
    chosen: dict


def held_photos(frozen, kept):
    """The photos a run leaves exactly as published: those of the listings whose choice record cannot be read (frozen),
    and the produce's kept set (publish.read_kept: the photos of the listings that left sale and keep their records)."""
    return {(l["source"], sha) for l in frozen for sha in l["photos"]} | set(kept)


def read_decisions(s3, root, listings):
    """The Decisions for these listings. The conflict set is the produce's (stages.images.link_standard), never
    re-classified; none stored yet: publish.NoConflictSet, nothing done."""
    exclude = publish.excluded()
    (owner, broken), clicks = owner_records(s3, root), publish.read_clicks(s3, root)
    conflicts = publish.read_conflicts(s3, {l["source"] for l in listings})
    rounds = select.read_rounds(s3, (C.RUN_TAG, C.CLEAN_TAG), sorted({l["source"] for l in listings}))
    chosen, broken_choices = choices(s3, root)
    # a listing whose own choice record or the owner's choice record cannot be read stays exactly as published
    unreadable = ({sku: f"the owner's choice record cannot be read ({why})" for sku, why in broken.items()}
                  | {l["sku"]: f"its choice record cannot be read ({broken_choices[l['key']]})" for l in listings
                     if l["key"] in broken_choices})
    picks = {l["key"]: pick for l in listings
             if (pick := select.owner_choice(owner.get(l["sku"]), l, exclude, conflicts, rounds)) is not None}
    picks |= {l["key"]: (r["tag"], r["sha"]) for l in listings
              if l["sku"] in unreadable and (r := chosen.get(l["key"])) is not None}
    return Decisions(exclude, owner, clicks, conflicts, rounds, picks, unreadable, chosen)


def unreadable_problems(frozen, decisions):
    """The run's problem line for each listing whose choice record cannot be read (`frozen`; every kind of run names
    them)."""
    return [f"{l['key']}: {decisions.unreadable[l['sku']]}: left exactly as published" for l in frozen]


def photos_round(s3, batch, spend, clean, run_id, deadline, root, say, listings, decisions, frozen, clicks_only,
                 restandardise=frozenset(), scoped=False):
    """Steps 1 to 7: pick the listings without a valid choice (never one the owner removed, nor one of `frozen`, whose
    choice record cannot be read: the owner's decision is unknown, so it is left exactly as published, its photos'
    pending redraws included), only those with the owner's click when clicks_only, work on them and report: (problems,
    counts, kept, {SKU: its review row}, the SKUs worked). A scoped run (scoped: `listings` are its scope's) also sends
    its listed photos to re-standardise (restandardise, {(source, sha)}) to the GPU once."""
    removed, records = removed_listings(s3), decisions.chosen
    workable = [l for l in listings if l not in frozen]
    found = todo(workable, records, decisions.exclude, whole_slabs(s3, records), removed, decisions.conflicts,
                 decisions.picks)
    if clicks_only:
        found = [l for l in found if l["sku"] in decisions.clicks]
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG, decisions.owner, decisions.conflicts, decisions.rounds,
                           live_choices(workable, records, removed, decisions), decisions.clicks,
                           {(l["source"], sha) for l in frozen for sha in l["photos"]}, restandardise, scoped)
    held_on = sum("kept" in l for l in found)
    say(f"==> standardise process {run_id}: {len(found) - held_on} listings without a chosen photo, {held_on} whose chosen "
        f"photo is no whole slab; {C.RUN_TAG} then {C.CLEAN_TAG}")
    problems, result = run(batch, sel, spend, clean, run_id, deadline, say, clicks_only=clicks_only)
    counts = {"listings": len(found), "chosen": len(result["chosen"]), "review": len(result["tracked"]),
              "pending": len(result["pending"]), "removed": sum(l["sku"] in removed for l in listings)}
    return problems, counts, {"pending_listings": result["pending"]}, result["review"], [l["sku"] for l in found]


def close_round(s3, batch, spend, run_id, deadline, root, say, frozen):
    """Instead of steps 1 to 7 (SCRAPER_STANDARDISE_CLOSE_ONLY): every chosen photo's close-up not made yet under the
    current rule made again, one close-only GPU job per slice of those (gpu.CLOSE_ROUND: the same sizing to the budget,
    reservation and window as any round; a photo already done never boots a GPU), never of a source whose image pipeline
    is off (its products show the scraped photos), never of a listing whose choice record cannot be read (`frozen`, its
    listing keys: it stays exactly as published): (problems, counts, kept)."""
    records = [r for r in close.chosen(s3, root) if r["key"] not in frozen]
    photos, todo = 0, {}
    for src in sorted({r["source"] for r in records} - image_pipeline_off()):
        listed = close.listed(records, src)
        photos += len({r["sha"] for r in listed})
        if left := sorted({r["sha"] for r in close.undone(s3, listed)}):
            todo[src] = left
    to_do = sum(len(shas) for shas in todo.values())
    say(f"==> close-up process {run_id}: {to_do} of {photos} chosen photos without a close-up v{C.CLOSEUP_VERSION}"
        f"{' of ' + ', '.join(todo) if todo else ''}")
    problems = []
    if todo:
        try:
            problems = [f"GPU job {name} failed: {why}" for name, why in
                        gpu.run_round(batch, s3, C.CLEAN_TAG, todo, run_id, deadline, spend, work=gpu.CLOSE_ROUND)]
        except (select.Halted, gpu.Skipped, select.SpendCapReached) as exc:
            problems = [f"close-up round not started: {exc}"]
        except TimeoutError as exc:                    # its jobs were stopped; what they finished stays
            problems = [f"close-up round stopped: {exc}"]
    return problems, {"close_only": True, "photos": photos, "to_do": to_do}, {}


def process(s3, batch, spend, clean, run_id, deadline, say=print, close_only=False, clicks_only=False, scope_key=None):
    """Clear, work (photos_round, or close_round when close_only), report and publish (steps 0 to 8), inside the lock: the
    run's summary (counts), also kept with what is pending and the problems under the clean tag (summary.json, or
    close-summary.json for a close-only run). Both publish only the conflict photos the owner assigned (assignments):
    a close-only run republishes the same choices, so it decides them the same way. At the end of the work window paid
    work stops (no hosted call or GPU round starts after it). clicks_only: steps 1 to 7 over the listings with the
    owner's click alone, its summary clicks-summary.json and its rows merged into the review list (merge_review).
    scope_key: a scoped run's scope (read_scope): steps 1 to 7 over the scope's listings in its order, its photos to
    re-standardise sent to the GPU once, its rows merged into the review list; the decisions, the conflict assignments
    and publish stay the whole sheet's, so no other listing is touched. A scope that cannot be worked is refused
    (ScopeRefused) before anything is paid."""
    if close_only and clicks_only:
        raise ValueError("a photos run is close-only or clicks-only, never both")
    if scope_key and (close_only or clicks_only):
        raise ValueError("a scoped run is neither close-only nor clicks-only")
    root = f"{ROOT}{C.CLEAN_TAG}/"
    sheet, photos = sheet_and_photos(s3)               # read once: the work step and publish (tiles) see the same sheet
    listings = photo_listings(sheet, photos)
    # A scope is read and checked before anything else is done, so one that is refused leaves everything as it was.
    scope = read_scope(s3, scope_key) if scope_key else None
    work = scoped(listings, scope)
    restandardise = restandardise_items(scope, work, scope_rounds(s3, scope, work)) if scope else frozenset()
    if left := gpu.clear_leftovers(batch, run_id):
        say(f"==> stopped {len(left)} GPU jobs an earlier run left")
    # No job writes now: a promotion cut short is finished, a frozen listing's too (decided before, only completed: a
    # half-written published photo would be worse; frozen means no new decision, see settle).
    if settled := select.finish(s3, (C.RUN_TAG, C.CLEAN_TAG)):
        say(f"==> finished {settled} outline rechecks an earlier run left half settled")
    decisions = read_decisions(s3, root, listings)
    kept_photos = publish.read_kept(s3)                # read before anything is paid: no publish without it
    frozen = [l for l in listings if l["sku"] in decisions.unreadable]   # left exactly as published, in every run kind
    assigned, conflicted = assignments(listings, decisions.picks, decisions.conflicts)
    closing = threading.Timer(max(0.0, deadline - time.monotonic()), halt, ((spend, clean), "the work window ended"))
    closing.daemon = True
    closing.start()
    try:
        if close_only:                                 # a close-up round reviews no listing: the review list stays
            problems, counts, kept = close_round(s3, batch, spend, run_id, deadline, root, say, {l["key"] for l in frozen})
            review = None
        else:
            problems, counts, kept, review, worked = photos_round(s3, batch, spend, clean, run_id, deadline, root, say,
                                                                  work, decisions, frozen, clicks_only, restandardise,
                                                                  scope is not None)
    finally:
        closing.cancel()
    problems += conflicted + unreadable_problems(frozen, decisions)
    kept_as_was = restandardise_kept(s3, restandardise, lambda src: round_prefix(C.RUN_TAG, src))
    problems += [f"re-standardise {r['source']} {r['sha'][:12]} kept as it was: {r['why']}" for r in kept_as_was]
    try:
        published = publish.publish(s3, root, sheet, photos, say, assigned=assigned, held=held_photos(frozen, kept_photos))
    except publish.NoChoiceRecords as exc:             # nothing chosen yet in this environment: nothing to publish
        problems.append(str(exc))
        published = {"changed": False}
    hidden = published.pop("tiles_hidden", {})
    problems += [f"tile {key}: {why}" for key, why in sorted(published.pop("tiles_problems", {}).items())]
    for p in problems:
        say(f"   {p}")
    summary = {"run_id": run_id, **counts, "problems": len(problems), "published": published,
               "run_spent_usd": round(spend.spent, 4), "month_spent_usd": round(spend.before + spend.spent, 4),
               "budget_usd": spend.budget, "budget_reached": spend.capped, **clean_summary(clean)}
    s3.put_object(Bucket=S3_BUCKET, Key=f"{root}{summary_file(close_only, clicks_only)}",
                  Body=json.dumps({**summary, **kept, "problem_list": problems, "tiles_hidden": hidden,
                                   "restandardise_kept": kept_as_was},
                                  sort_keys=True).encode(),
                  ContentType="application/json")
    if clicks_only or scope is not None:
        merge_review(s3, root, review, worked, hidden, tile_listings(sheet, photos))
    elif review is not None:
        write_review(s3, root, review, hidden, tile_listings(sheet, photos))
    return summary


class ReviewUnreadable(RuntimeError):
    """The stored review list is not a JSON object of rows: never read as empty, so a merge never drops its rows."""


def read_review(s3, root):
    """{SKU: row} of the stored review list (review.json); {} when there is none yet. ReviewUnreadable otherwise."""
    key = f"{root}{C.REVIEW_FILE}"
    try:
        body = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
    except ClientError as exc:
        if _code(exc) in publish.MISSING:
            return {}
        raise
    try:
        rows = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ReviewUnreadable(f"the review list {key} cannot be read: {exc}") from exc
    if not isinstance(rows, dict):
        raise ReviewUnreadable(f"the review list {key} is not an object of rows")
    return rows


def _put_review(s3, root, rows):
    s3.put_object(Bucket=S3_BUCKET, Key=f"{root}{C.REVIEW_FILE}", Body=json.dumps(rows, sort_keys=True).encode(),
                  ContentType="application/json")


def summary_file(close_only, clicks_only):
    """The name of a run's summary under the clean tag: each kind of photos run keeps its own."""
    return "close-summary.json" if close_only else "clicks-summary.json" if clicks_only else "summary.json"


def tile_rows(tiles_hidden, tiles):
    """{SKU: row} of the tiles publish could not render (tile_no_stone, publish's why)."""
    by_key = {l["key"]: l for l in tiles}
    return {by_key[key]["sku"]: {"listing": key, "source": by_key[key]["source"], "format": C.TILE_FORMAT,
                                 "reason": "tile_no_stone", "why": why, "click": None, "photos": []}
            for key, why in tiles_hidden.items()}


def merge_review(s3, root, review, worked, tiles_hidden, tiles):
    """A clicks-only run's rows merged into the review list (manager ruling C1, 2026-11-07): the rows of the SKUs it
    worked replaced by its own (a listing now chosen loses its row), the tiles' rows by its publish's (publish renders
    every tile in every run kind: a tile it rendered loses its row), every other row left as stored (the same bytes,
    the file sorted by SKU). Inside the run lock, like every write of the process."""
    gone = set(worked)
    kept = {sku: row for sku, row in read_review(s3, root).items()
            if sku not in gone and row["reason"] != "tile_no_stone"}
    _put_review(s3, root, {**kept, **review, **tile_rows(tiles_hidden, tiles)})


def write_review(s3, root, review, tiles_hidden, tiles):
    """The review list (review.json, after publish: only publish knows the tiles it could not render): this run's rows
    (select.review_row) and a row per hidden tile (tile_rows), by SKU, the same bytes for the same rows."""
    _put_review(s3, root, {**review, **tile_rows(tiles_hidden, tiles)})


def clean_summary(clean):
    """The cleaning cap's part of the run's summary: what this run and the month spent on edits and their checks, the
    cap, whether it was reached, and how many paid calls the run listed. All None when no cap is set."""
    if clean is None:
        return {"clean_run_spent_usd": None, "clean_month_spent_usd": None, "clean_budget_usd": None,
                "clean_budget_reached": None, "clean_uses": None}
    return {"clean_run_spent_usd": round(clean.spent, 4), "clean_month_spent_usd": round(clean.before + clean.spent, 4),
            "clean_budget_usd": clean.budget, "clean_budget_reached": clean.capped, "clean_uses": clean.paid_calls()}


def relink():
    """produce --stage republish, so the products link the photos just published (the image stage reads the published
    folder). In this process, never a child: a run the runner stops takes its relink with it, so no produce goes on
    outside the run slot. Its stdout is image-progress noise, dropped as the runner drops it for every produce; its log
    goes to stderr. The pipeline is imported only when a relink is due."""
    from stone_pipeline import produce
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
        return produce.run(None, "republish")


def reap(run_id):
    """After the runner killed the photo process of run `run_id` (it outlived the give-way's deadline, so its own cleanup
    never ran): stop that run's GPU jobs and, only while the run lock is still that run's (its body names the run), set
    the pool's ceiling back and remove the lock. Nothing of another process is touched: a rolling deploy's other task or
    a process started by hand keeps its jobs, its lock and its ceiling."""
    s3, batch = aws_client("s3", config=PROCESS_RETRIES), aws_client("batch", config=PROCESS_RETRIES)
    stopped = gpu.stop_run(batch, run_id)
    try:
        held = s3.get_object(Bucket=S3_BUCKET, Key=LOCK)
    except ClientError as exc:
        if _code(exc) not in publish.MISSING:
            raise
        held = None
    ours = held is not None and json.loads(held["Body"].read()).get("run_id") == run_id
    if ours:
        gpu.set_idle(batch)
        _release(s3, held["ETag"])
    print(f"==> cleared what killed run {run_id} left: {len(stopped)} GPU jobs stopped"
          + ("; its lock removed, the GPU pool back to idle" if ours else "; the lock is another process's, left as it is"),
          flush=True)


def unconfigured(close_only=False, scoped=False):
    """What is missing or wrong in the settings: the GPU target and the pool's idle ceiling, the hosted key (not for a
    close-only run: it makes no hosted call), the month's budget, and a work window that leaves the GPU rounds their minimum after the judge's share
    (not for a close-only run: it judges nothing) and that, plus its finish, must end before the runner's ceiling."""
    missing = []
    for name in ("GPU_QUEUE", "STANDARDISE_JOBDEF"):          # what gpu.target() reads, each named when missing
        try:
            env.require(name)
        except KeyError as exc:
            missing.append(exc.args[0])
    if C.IDLE_GPU_VCPUS is None:                               # what every round sets the pool back to (gpu.idle_vcpus)
        missing.append("SCRAPER_STANDARDISE_IDLE_GPU_VCPUS")
    try:
        if not close_only:
            judge.fal_key()
    except RuntimeError:
        missing.append("FAL_KEY")
    if C.BUDGET_USD is None:
        missing.append("SCRAPER_STANDARDISE_BUDGET_USD")
    if scoped and C.RUN_CAP_USD is None:                       # a scoped run's cap binds whatever the months leave
        missing.append("SCRAPER_STANDARDISE_RUN_CAP_USD (required with SCRAPER_STANDARDISE_SCOPE_KEY)")
    if not close_only and C.WORK_SECONDS < C.JUDGE_WINDOW_SECONDS + C.GPU_MIN_WINDOW_SECONDS:   # close-only: no judge
        missing.append(f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) at least JUDGE_WINDOW_SECONDS "
                       f"({C.JUDGE_WINDOW_SECONDS}) + GPU_MIN_WINDOW_SECONDS ({C.GPU_MIN_WINDOW_SECONDS})")
    if C.WORK_SECONDS + C.FINISH_SECONDS >= RUN_TIMEOUT_SECONDS:
        missing.append(f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) + FINISH_SECONDS ({C.FINISH_SECONDS}) "
                       f"under SCRAPER_RUN_TIMEOUT_SECONDS ({RUN_TIMEOUT_SECONDS})")
    return missing


def cleaning_cap(s3, run_id, close_only, run=None):
    """This run's Spend against the month's cleaning cap (owner 2026-11-05: the hosted clean-up edits and their checks
    have their own cap), or None when no edit can be paid: no cap set (said once, here), or a close-only run (it makes
    no hosted call)."""
    if close_only:
        return None
    if C.CLEAN_BUDGET_USD is None:
        print("==> no cleaning cap (SCRAPER_STANDARDISE_CLEAN_BUDGET_USD unset): no photo is sent to a paid clean-up edit "
              "this run", flush=True)
        return None
    return month_spend(s3, run_id, C.CLEAN_BUDGET_USD, C.CLEAN_SPEND_DIR, select.CleanCapReached, run)


def hand_over(summary):
    """The run's summary to the runner (SCRAPER_RUN_SUMMARY), for its run record; nothing when run by hand."""
    path = env.getenv("RUN_SUMMARY")
    if path:
        with open(path, "w") as out:
            json.dump(summary, out, sort_keys=True)


def main() -> int:
    close_only, clicks_only = env.env_bool("STANDARDISE_CLOSE_ONLY"), env.env_bool("STANDARDISE_CLICKS_ONLY")
    if close_only and clicks_only:
        print("ERROR: SCRAPER_STANDARDISE_CLOSE_ONLY and SCRAPER_STANDARDISE_CLICKS_ONLY are both set: a photos run is "
              "one kind; aborting")
        return 2
    scope_key = env.getenv("STANDARDISE_SCOPE_KEY")    # a scoped run (process): its scope's S3 key
    if scope_key and (close_only or clicks_only):
        print("ERROR: SCRAPER_STANDARDISE_SCOPE_KEY is set on a close-only or clicks-only run: a scoped run is a full "
              "run of its listings; aborting")
        return 2
    if missing := unconfigured(close_only, scoped=bool(scope_key)):
        print(f"ERROR: not configured: {', '.join(missing)}; aborting")
        return 2
    deadline = time.monotonic() + C.WORK_SECONDS
    s3, batch = aws_client("s3", config=PROCESS_RETRIES), aws_client("batch", config=PROCESS_RETRIES)
    # The runner's run id names the lock and the GPU jobs, so the runner can clear exactly what this run left if it
    # has to kill it; a process started by hand names itself.
    run_id = env.getenv("RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_cap = select.RunCap(C.RUN_CAP_USD) if scope_key else None   # both budgets reserve against it
    spend = month_spend(s3, run_id, C.BUDGET_USD, C.SPEND_DIR, run=run_cap)
    clean = cleaning_cap(s3, run_id, close_only, run_cap)
    stop_on_signal(spend, clean)                       # before the lock: a stop at any point releases it
    try:
        with exclusive(s3, {"run_id": run_id, "tags": [C.RUN_TAG, C.CLEAN_TAG], "close_only": close_only,
                            "clicks_only": clicks_only},
                       on_lost=lambda: halt((spend, clean), "the run lock was taken over")):
            summary = process(s3, batch, spend, clean, run_id, deadline, close_only=close_only, clicks_only=clicks_only,
                              scope_key=scope_key)
    except RunActive as exc:
        print(f"ERROR: {exc}; not started")
        return 4
    except (publish.NoConflictSet, publish.NoKeptSet, ScopeRefused) as exc:
        print(f"ERROR: {exc}; nothing done")
        return 2
    print(f"==> spent ${spend.spent:.2f} this run, ${spend.before + spend.spent:.2f} of ${spend.budget:.2f} this month"
          + ("; the month's budget is reached: the rest waits for next month or a higher budget" if spend.capped else ""),
          flush=True)
    if clean is not None:
        print(f"==> cleaning: spent ${clean.spent:.2f} this run in {clean.paid_calls()} paid calls, "
              f"${clean.before + clean.spent:.2f} of ${clean.budget:.2f} this month"
              + ("; the cleaning cap is reached: further edits wait for next month or a higher cap" if clean.capped else ""),
              flush=True)
    rc, summary["relinked"] = 0, False
    if summary["published"]["changed"]:
        if not writethrough.enabled():
            print("==> published; no ledger here, the next produce links the new photos", flush=True)
        elif (rc := relink()) != 0:
            print(f"==> FAILED to link the new photos: the republish exited {rc}", flush=True)
        else:
            summary["relinked"] = True
            print("==> the new photos are linked (republish done)", flush=True)
    # A ceiling lowered while a round's instances still ran stays at what they held (Batch never sets it below them), so
    # it is set once more now, after publish and relink, when the pool has drained. Not failing the run: its photos are
    # published, and the next run sets the ceiling again before its first round (clear_leftovers).
    try:
        gpu.set_idle(batch)
    except (BotoCoreError, ClientError) as exc:
        print(f"ERROR: the GPU pool's ceiling could not be set back to idle ({exc}); the next run sets it", flush=True)
    hand_over(summary)
    return rc


if __name__ == "__main__":
    sys.exit(main())
