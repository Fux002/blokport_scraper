"""The standardised-photo process of an environment (STANDARDISE_PROCESS.md): every listing of the product sheet without
a valid chosen photo ends with one good photo, published and linked, or on the review list with the reason. The
runner's `photos` stage, started after each full produce on the owner's button. In order:
  0. clear: GPU jobs an earlier, killed run left are stopped and the GPU pool's ceiling is set back to idle
  1. pick: the sheet's listings without a valid choice record (missing, its photo no longer the listing's or excluded,
     not the owner's choice, or a photo listings of several stones show that the owner did not assign), never one the
     owner removed; and the slab listings whose chosen photo is no whole slab (a close-up: no 3D slab), their choice kept;
     only of the sources whose image pipeline is on
  2. GPU: their first photos the standardiser has not run yet (deploy.standardise_batch), under STANDARDISE.RUN_TAG,
     and every chosen result outdated (a whole slab drawn under an older outline rule: live_choices), its listing not
     picked again; then settle: a recheck drawn differently is a candidate beside its result, judged as a new result
     is, promoted over it on a pass or discarded on a fail (select.settle)
  3. judge: the owner's choice of a listing, else its results in its own order until one passes
  4. clean: a hosted edit of one fixable photo of each listing still without a pass
  5. GPU: the cleaned copies, through every step a scraped photo takes, under STANDARDISE.CLEAN_TAG
  6. judge: the cleaned copies' results
  7. report: one choice record per listing, tracked.csv (the review list) and summary.json, under the clean tag
  8. publish: the published folder made to match the choice records (deploy.standardise_publish)
  9. relink: when the folder changed, produce --stage republish, so the new photos are linked in this same run
With SCRAPER_STANDARDISE_CLOSE_ONLY on (the runner sets it for a close_only photos run), steps 1 to 7 are one close-up
round instead: every chosen photo's close-up not made yet under the current rule made again from the photo its run
started from (deploy.standardise_close, a GPU job per slice, under the same lock, budget, window and run id), then
publish and relink as above; its summary is close-summary.json.
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
SCRAPER_STANDARDISE_CLOSE_ONLY (set by the runner, true only for a close_only run): the close-up round instead of 1 to 7.
Exit 0 the run went through (pending listings, problems and a reached budget are in its summary); 2 not configured,
or no produce has stored the conflict set yet;
4 another process is running; a failed relink exits with the produce's code; any other failure is loud (a traceback).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from deploy import standardise_batch as gpu
from deploy import standardise_close as close
from deploy import standardise_publish as publish
from deploy import standardise_select as select
from deploy.standardise_source import sheet_and_photos
from stone_pipeline.config.settings import ENV_SEGMENT, RUN_TIMEOUT_SECONDS, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.config.sources import image_pipeline_off
from stone_pipeline.core import env
from stone_pipeline.io import imagestore
from stone_pipeline.ledger import writethrough
from stone_pipeline.standardise import index, judge

ROOT = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/"
LOCK = f"{ROOT}run.lock"
_TAKEN = ("PreconditionFailed", "ConditionalRequestConflict")
_MISSING = ("404", "NoSuchKey", "NotFound")


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
    if _code(exc) == "PreconditionFailed" or _code(exc) in _MISSING:
        return True
    if _code(exc) != "ConditionalRequestConflict":
        return False
    try:
        return s3.head_object(Bucket=S3_BUCKET, Key=LOCK)["ETag"] != etag
    except ClientError as missing:
        return _code(missing) in _MISSING
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
            if _code(exc) in _MISSING:
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
            if _code(exc) not in _TAKEN + _MISSING:
                raise
            if _lost(s3, etag, exc):
                print("==> the run lock is no longer this run's: left as it is", flush=True)
                return
    print("==> the run lock could not be removed (writes in flight): it is taken over once stale", flush=True)


# --- the month's budget -----------------------------------------------------------------------------------------------
def spend_prefix(directory, month):
    return f"{ROOT}{directory}/{month}/"


def month_spend(s3, run_id, budget, directory, cap_error=select.SpendCapReached):
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
    return select.Spend(budget, others, save, cap_error)


# --- what a run works on ----------------------------------------------------------------------------------------------
def choices(s3, root):
    """{listing key: its choice record} under the clean tag's root."""
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=publish.chosen_prefix(root))
            for o in page.get("Contents", [])]
    records = publish.pool_map(publish.WORKERS, lambda k: json.loads(s3.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read()), keys)
    return {r["key"]: r for r in records}


def whole_slabs(s3, records):
    """The <sha>.slab.png keys in the run folders the choice records point at: which chosen photos are a whole slab."""
    out = set()
    for prefix in sorted({r["prefix"] for r in records.values()}):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            out.update(o["Key"] for o in page.get("Contents", []) if o["Key"].endswith(".slab.png"))
    return out


def photo_listings(sheet, photos):
    """The sheet's listings shown from their own photos (index.listings): every one but a tile listing, which is
    rendered from its variety's slab stone at publish (deploy.standardise_publish), never through the GPU and judge."""
    return [l for l in index.listings(sheet, photos) if l["format"] != C.TILE_FORMAT]


def removed_listings(s3):
    """The SKUs of the listings the owner removed (deploy.standardise_decisions)."""
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=imagestore.owner_removed_prefix())
            for o in page.get("Contents", [])]
    return imagestore.owner_removed_skus(keys)


def owner_records(s3, root):
    """({SKU: the owner's photo choice record}, {SKU: why its record could not be read}) under the clean tag's root
    (deploy.standardise_decisions)."""
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
    the work window or stuck, a round skipped (too little window left, or the budget left covers not one slice: the
    hosted steps still judge what is finished, within their own reservations), listed photos left without a result.
    stopped: paid work has ended (the window, a stop, a lost lock), so no step after it starts."""
    try:
        problems = [f"GPU job {name} failed: {why}"
                    for name, why in gpu.run_round(batch, sel.client, tag, todo, run_id, deadline, spend)]
    except select.Halted as exc:
        return [f"GPU round {tag} not started: {exc}"], True
    except (gpu.Skipped, select.SpendCapReached) as exc:
        return [f"GPU round {tag} not started: {exc}"], False
    except TimeoutError as exc:                        # its jobs were stopped; what they finished stays
        problems = [f"GPU round {tag} stopped: {exc}"]
    sel.refresh()
    rnd = dict(sel.rounds)[tag]
    problems += [f"{src} {sha[:12]}: no result after the GPU round" for src, shas in todo.items() for sha in shas
                 if select.needs_gpu(rnd[src], sha)]
    return problems, False


def hosted(step, sel, spend, deadline):
    """A hosted step over every listing, run a second time for the listings that failed (a passing hiccup): (problems,
    stopped)."""
    failures, stopped = step(sel, spend, deadline)
    if failures and not stopped:
        failures, stopped = step(sel, spend, deadline)
    return [f"{key}: {why}" for key, why in failures], stopped


def run(batch, sel, spend, clean, run_id, deadline, say=print, clock=time.monotonic):
    """The GPU and hosted steps, then the report: (problems, report). A step starts only inside the work window and while
    the budget lasts (the hosted clean-up edits and their checks on the cleaning cap, `clean`: None when it is not set,
    and then no photo is sent to an edit; the clean switch governs only the automatic edits, select.authorised); the GPU rounds end JUDGE_WINDOW_SECONDS before it, so the judge always has its share; after each
    GPU round the outline recheck's candidates are settled (select.settle); the report always runs, so what is chosen is
    recorded and published, unless a promotion failed: raised, and the next run of either kind finishes it at its start
    (select.finish, in process) before anything is published."""
    gpu_deadline = deadline - C.JUDGE_WINDOW_SECONDS
    steps = [lambda: gpu_round(batch, sel, sel.tags[0], sel.gpu_todo(), run_id, gpu_deadline, spend),
             lambda: hosted(select.settle, sel, spend, deadline),
             lambda: hosted(select.judge_all, sel, spend, deadline),
             *([lambda: hosted(select.clean_all, sel, clean, deadline)] if clean is not None else []),
             lambda: gpu_round(batch, sel, sel.tags[1], sel.clean_todo(), run_id, gpu_deadline, spend),
             lambda: hosted(select.settle, sel, spend, deadline),
             lambda: hosted(select.judge_all, sel, spend, deadline)]
    problems = []
    for step in steps:
        if clock() >= deadline:
            problems.append(f"the work window ({C.WORK_SECONDS}s) ended: the next run goes on")
            break
        found, stopped = step()
        problems += found
        if stopped:
            break
    result = select.report(sel)
    by_tag = {tag: sum(c["tag"] == tag for c in result["chosen"]) for tag in sel.tags}
    say(f"==> {len(result['chosen'])} of {len(sel.listings)} listings with a good photo {by_tag}; "
        f"{len(result['tracked'])} on the review list (s3://{S3_BUCKET}/{sel.root}tracked.csv), {len(result['pending'])} pending")
    if result["pending"]:
        problems.append(f"{len(result['pending'])} listings not fully judged (listed in {sel.root}summary.json)")
    problems += result["click_problems"]                 # a click that cannot be used, for any listing of the run
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


def read_decisions(s3, root, listings):
    """The Decisions for these listings. The conflict set is the produce's (stages.images.link_standard), never
    re-classified; none stored yet: publish.NoConflictSet, nothing done."""
    exclude = publish.read_exclude(publish.EXCLUDE_PATH.read_text())
    (owner, unreadable), clicks = owner_records(s3, root), publish.read_clicks(s3, root)
    conflicts = publish.read_conflicts(s3, {l["source"] for l in listings})
    rounds = select.read_rounds(s3, (C.RUN_TAG, C.CLEAN_TAG), sorted({l["source"] for l in listings}))
    chosen = choices(s3, root)
    picks = {l["key"]: pick for l in listings
             if (pick := select.owner_choice(owner.get(l["sku"]), l, exclude, conflicts, rounds)) is not None}
    picks |= {l["key"]: (r["tag"], r["sha"]) for l in listings
              if l["sku"] in unreadable and (r := chosen.get(l["key"])) is not None}
    return Decisions(exclude, owner, clicks, conflicts, rounds, picks, unreadable, chosen)


def unreadable_problems(listings, decisions):
    """The run's problem line for each listing whose choice record cannot be read (every kind of run names them)."""
    return [f"{l['key']}: the owner's choice record cannot be read ({decisions.unreadable[l['sku']]}): left exactly as "
            "published" for l in listings if l["sku"] in decisions.unreadable]


def photos_round(s3, batch, spend, clean, run_id, deadline, root, say, listings, decisions):
    """Steps 1 to 7: pick the listings without a valid choice (never one the owner removed, nor one whose choice record
    cannot be read: the owner's decision is unknown, so it is left exactly as published), work on them and report:
    (problems, counts, kept)."""
    removed, records = removed_listings(s3), decisions.chosen
    workable = [l for l in listings if l["sku"] not in decisions.unreadable]
    found = todo(workable, records, decisions.exclude, whole_slabs(s3, records), removed, decisions.conflicts,
                 decisions.picks)
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG, decisions.owner, decisions.conflicts, decisions.rounds,
                           live_choices(workable, records, removed, decisions), decisions.clicks)
    held_on = sum("kept" in l for l in found)
    say(f"==> standardise process {run_id}: {len(found) - held_on} listings without a chosen photo, {held_on} whose chosen "
        f"photo is no whole slab; {C.RUN_TAG} then {C.CLEAN_TAG}")
    problems, result = run(batch, sel, spend, clean, run_id, deadline, say)
    counts = {"listings": len(found), "chosen": len(result["chosen"]), "review": len(result["tracked"]),
              "pending": len(result["pending"]), "removed": sum(l["sku"] in removed for l in listings)}
    return problems, counts, {"pending_listings": result["pending"]}


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


def process(s3, batch, spend, clean, run_id, deadline, say=print, close_only=False):
    """Clear, work (photos_round, or close_round when close_only), report and publish (steps 0 to 8), inside the lock: the
    run's summary (counts), also kept with what is pending and the problems under the clean tag (summary.json, or
    close-summary.json for a close-only run). Both publish only the conflict photos the owner assigned (assignments):
    a close-only run republishes the same choices, so it decides them the same way. At the end of the work window paid
    work stops (no hosted call or GPU round starts after it)."""
    root = f"{ROOT}{C.CLEAN_TAG}/"
    if left := gpu.clear_leftovers(batch, run_id):
        say(f"==> stopped {len(left)} GPU jobs an earlier run left")
    if settled := select.finish(s3, (C.RUN_TAG, C.CLEAN_TAG)):    # no job writes now: a promotion cut short is finished
        say(f"==> finished {settled} outline rechecks an earlier run left half settled")
    sheet, photos = sheet_and_photos(s3)               # read once: the work step and publish (tiles) see the same sheet
    listings = photo_listings(sheet, photos)
    decisions = read_decisions(s3, root, listings)
    assigned, conflicted = assignments(listings, decisions.picks, decisions.conflicts)
    closing = threading.Timer(max(0.0, deadline - time.monotonic()), halt, ((spend, clean), "the work window ended"))
    closing.daemon = True
    closing.start()
    try:
        if close_only:
            frozen = {l["key"] for l in listings if l["sku"] in decisions.unreadable}
            problems, counts, kept = close_round(s3, batch, spend, run_id, deadline, root, say, frozen)
        else:
            problems, counts, kept = photos_round(s3, batch, spend, clean, run_id, deadline, root, say, listings, decisions)
    finally:
        closing.cancel()
    problems += conflicted + unreadable_problems(listings, decisions)
    try:
        published = publish.publish(s3, root, sheet, photos, say, assigned=assigned)
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
    s3.put_object(Bucket=S3_BUCKET, Key=f"{root}{'close-summary' if close_only else 'summary'}.json",
                  Body=json.dumps({**summary, **kept, "problem_list": problems, "tiles_hidden": hidden},
                                  sort_keys=True).encode(),
                  ContentType="application/json")
    return summary


def clean_summary(clean):
    """The cleaning cap's part of the run's summary: what this run and the month spent on edits and their checks, the
    cap, whether it was reached, and how many paid calls the run listed. All None when no cap is set."""
    if clean is None:
        return {"clean_run_spent_usd": None, "clean_month_spent_usd": None, "clean_budget_usd": None,
                "clean_budget_reached": None, "clean_uses": None}
    return {"clean_run_spent_usd": round(clean.spent, 4), "clean_month_spent_usd": round(clean.before + clean.spent, 4),
            "clean_budget_usd": clean.budget, "clean_budget_reached": clean.capped, "clean_uses": len(clean.uses)}


def relink():
    """produce --stage republish, so the products link the photos just published (the image stage reads the published
    folder). In this process, never a child: a run the runner stops takes its relink with it, so no produce goes on
    outside the run slot. Its stdout is image-progress noise, dropped as the runner drops it for every produce; its log
    goes to stderr. The pipeline is imported only when a relink is due."""
    from stone_pipeline import produce
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
        return produce.main(["--stage", "republish"])


def reap(run_id):
    """After the runner killed the photo process of run `run_id` (it outlived the give-way's deadline, so its own cleanup
    never ran): stop that run's GPU jobs and, only while the run lock is still that run's (its body names the run), set
    the pool's ceiling back and remove the lock. Nothing of another process is touched: a rolling deploy's other task or
    a process started by hand keeps its jobs, its lock and its ceiling."""
    retries = Config(retries={"mode": "standard", "max_attempts": 10})
    s3 = boto3.client("s3", region_name=S3_REGION, config=retries)
    batch = boto3.client("batch", region_name=S3_REGION, config=retries)
    stopped = gpu.stop_run(batch, run_id)
    try:
        held = s3.get_object(Bucket=S3_BUCKET, Key=LOCK)
    except ClientError as exc:
        if _code(exc) not in _MISSING:
            raise
        held = None
    ours = held is not None and json.loads(held["Body"].read()).get("run_id") == run_id
    if ours:
        gpu.set_idle(batch)
        _release(s3, held["ETag"])
    print(f"==> cleared what killed run {run_id} left: {len(stopped)} GPU jobs stopped"
          + ("; its lock removed, the GPU pool back to idle" if ours else "; the lock is another process's, left as it is"),
          flush=True)


def unconfigured(close_only=False):
    """What is missing or wrong in the settings: the GPU target, the hosted key (not for a close-only run: it makes no
    hosted call), the month's budget, and a work window that leaves the GPU rounds their minimum after the judge's share
    (not for a close-only run: it judges nothing) and that, plus its finish, must end before the runner's ceiling."""
    missing = []
    for name in ("GPU_QUEUE", "STANDARDISE_JOBDEF"):          # what gpu.target() reads, each named when missing
        try:
            env.require(name)
        except KeyError as exc:
            missing.append(exc.args[0])
    try:
        if not close_only:
            judge.fal_key()
    except RuntimeError:
        missing.append("FAL_KEY")
    if C.BUDGET_USD is None:
        missing.append("SCRAPER_STANDARDISE_BUDGET_USD")
    if not close_only and C.WORK_SECONDS < C.JUDGE_WINDOW_SECONDS + C.GPU_MIN_WINDOW_SECONDS:   # close-only: no judge
        missing.append(f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) at least JUDGE_WINDOW_SECONDS "
                       f"({C.JUDGE_WINDOW_SECONDS}) + GPU_MIN_WINDOW_SECONDS ({C.GPU_MIN_WINDOW_SECONDS})")
    if C.WORK_SECONDS + C.FINISH_SECONDS >= RUN_TIMEOUT_SECONDS:
        missing.append(f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) + FINISH_SECONDS ({C.FINISH_SECONDS}) "
                       f"under SCRAPER_RUN_TIMEOUT_SECONDS ({RUN_TIMEOUT_SECONDS})")
    return missing


def cleaning_cap(s3, run_id, close_only):
    """This run's Spend against the month's cleaning cap (owner 2026-11-05: the hosted clean-up edits and their checks
    have their own cap), or None when no edit can be paid: no cap set (said once, here), or a close-only run (it makes
    no hosted call)."""
    if close_only:
        return None
    if C.CLEAN_BUDGET_USD is None:
        print("==> no cleaning cap (SCRAPER_STANDARDISE_CLEAN_BUDGET_USD unset): no photo is sent to a paid clean-up edit "
              "this run", flush=True)
        return None
    return month_spend(s3, run_id, C.CLEAN_BUDGET_USD, C.CLEAN_SPEND_DIR, select.CleanCapReached)


def hand_over(summary):
    """The run's summary to the runner (SCRAPER_RUN_SUMMARY), for its run record; nothing when run by hand."""
    path = env.getenv("RUN_SUMMARY")
    if path:
        with open(path, "w") as out:
            json.dump(summary, out, sort_keys=True)


def main() -> int:
    close_only = env.env_bool("STANDARDISE_CLOSE_ONLY")
    if missing := unconfigured(close_only):
        print(f"ERROR: not configured: {', '.join(missing)}; aborting")
        return 2
    deadline = time.monotonic() + C.WORK_SECONDS
    retries = Config(retries={"mode": "standard", "max_attempts": 10})     # a throttled call is retried, never fatal
    s3 = boto3.client("s3", region_name=S3_REGION, config=retries)
    batch = boto3.client("batch", region_name=S3_REGION, config=retries)
    # The runner's run id names the lock and the GPU jobs, so the runner can clear exactly what this run left if it
    # has to kill it; a process started by hand names itself.
    run_id = env.getenv("RUN_ID") or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spend = month_spend(s3, run_id, C.BUDGET_USD, C.SPEND_DIR)
    clean = cleaning_cap(s3, run_id, close_only)
    stop_on_signal(spend, clean)                       # before the lock: a stop at any point releases it
    try:
        with exclusive(s3, {"run_id": run_id, "tags": [C.RUN_TAG, C.CLEAN_TAG], "close_only": close_only},
                       on_lost=lambda: halt((spend, clean), "the run lock was taken over")):
            summary = process(s3, batch, spend, clean, run_id, deadline, close_only=close_only)
    except RunActive as exc:
        print(f"ERROR: {exc}; not started")
        return 4
    except publish.NoConflictSet as exc:
        print(f"ERROR: {exc}; nothing done")
        return 2
    print(f"==> spent ${spend.spent:.2f} this run, ${spend.before + spend.spent:.2f} of ${spend.budget:.2f} this month"
          + ("; the month's budget is reached: the rest waits for next month or a higher budget" if spend.capped else ""),
          flush=True)
    if clean is not None:
        print(f"==> cleaning: spent ${clean.spent:.2f} this run in {len(clean.uses)} paid calls, "
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
