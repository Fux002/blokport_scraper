"""The standardised-photo process of an environment (STANDARDISE_PROCESS.md): every listing of the product sheet without
a valid chosen photo ends with one good photo, published and linked, or on the review list with the reason. The
runner's `photos` stage, started after each full produce on the owner's button. In order:
  0. clear: GPU jobs an earlier, killed run left are stopped and the GPU pool's ceiling is set back to idle
  1. pick: the sheet's listings without a valid choice record (missing, its photo no longer the listing's, or excluded)
  2. GPU: their first photos the standardiser has not run yet (deploy.standardise_batch), under STANDARDISE.RUN_TAG
  3. judge: each listing's results in its own order until one passes
  4. clean: a hosted edit of one fixable photo of each listing still without a pass
  5. GPU: the cleaned copies, through every step a scraped photo takes, under STANDARDISE.CLEAN_TAG
  6. judge: the cleaned copies' results
  7. report: one choice record per listing, tracked.csv (the review list) and summary.json, under the clean tag
  8. publish: the published folder made to match the choice records (deploy.standardise_publish)
  9. relink: when the folder changed, produce --stage republish, so the new photos are linked in this same run
Every step stores what it did, so a run that stops is run again and goes on where it stopped, paying nothing twice. The
GPU and hosted steps work at most STANDARDISE.WORK_SECONDS: after it no hosted call and no GPU round starts. Hosted calls
and GPU rounds together never take the month past STANDARDISE.BUDGET_USD (each run's spend of the month is its own file
under <env>/standardise-test/spend/<YYYY-MM>/).
One process at a time per environment: it holds <env>/standardise-test/run.lock (written only if absent, rewritten every
LOCK_HEARTBEAT_SECONDS while it runs, taken over only when not rewritten for the runner's ceiling: its run was killed)
and removes it, if it is still its own, when publish has ended. SIGTERM (the service stopping) unwinds the same way: the
round's jobs are stopped, the ceiling set back, the spend kept, the lock released.
Env: SCRAPER_GPU_QUEUE, SCRAPER_STANDARDISE_JOBDEF, FAL_KEY, SCRAPER_STANDARDISE_BUDGET_USD (all required);
SCRAPER_RUN_SUMMARY (set by the runner): where the run's summary is handed back for its run record.
Exit 0 the run went through (pending listings, problems and a reached budget are in its summary); 2 not configured;
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
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from deploy import standardise_batch as gpu
from deploy import standardise_publish as publish
from deploy import standardise_select as select
from deploy.standardise_source import sheet_listings
from stone_pipeline.config.settings import ENV_SEGMENT, RUN_TIMEOUT_SECONDS, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.config.sources import load_source
from stone_pipeline.core import env
from stone_pipeline.ledger import writethrough
from stone_pipeline.standardise import judge

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
    """Whether a failed conditional write means the lock is no longer this run's. PreconditionFailed says so; a 409
    (ConditionalRequestConflict: another write was in flight) is checked against the lock's ETag; any other failure is a
    passing one."""
    if not isinstance(exc, ClientError) or _code(exc) not in _TAKEN:
        return False
    if _code(exc) == "PreconditionFailed":
        return True
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
            if _code(exc) in _MISSING or _lost(s3, etag, exc):
                print("==> the run lock is no longer this run's: left as it is", flush=True)
                return
    print("==> the run lock could not be removed (writes in flight): it is taken over once stale", flush=True)


# --- the month's budget -----------------------------------------------------------------------------------------------
def spend_prefix(month):
    return f"{ROOT}{C.SPEND_DIR}/{month}/"


def month_spend(s3, run_id):
    """This run's Spend against the month's budget. Each run keeps only its own file (<run id>.json, {"usd"}), so no run
    can overwrite another's; the month's other runs are summed from their files, read again only when changed (ETag).
    The month is the run's start (UTC), from its id (YYYYMMDDTHHMMSSZ)."""
    prefix = spend_prefix(f"{run_id[:4]}-{run_id[4:6]}")
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

    def save(total):
        s3.put_object(Bucket=S3_BUCKET, Key=own, Body=json.dumps({"usd": round(total, 6)}).encode(),
                      ContentType="application/json")
    return select.Spend(C.BUDGET_USD, others, save)


# --- what a run works on ----------------------------------------------------------------------------------------------
def choices(s3, root):
    """{listing key: its choice record} under the clean tag's root."""
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=publish.chosen_prefix(root))
            for o in page.get("Contents", [])]
    records = publish.pool_map(publish.WORKERS, lambda k: json.loads(s3.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read()), keys)
    return {r["key"]: r for r in records}


def todo(listings, records, exclude):
    """The listings without a valid chosen photo: no record, or its photo is no longer one of the listing's (the sheet
    changed) or is on the exclude list. A listing on the review list has no record, so every run looks at it again (its
    stored answers cost nothing; a change that can help it reaches it)."""
    def chosen(l):
        r = records.get(l["key"])
        return r is not None and r["sha"] in l["photos"] and r["sha"] not in exclude
    return [l for l in listings if not chosen(l)]


# --- the steps --------------------------------------------------------------------------------------------------------
def stop_on_signal(spend):
    """SIGTERM (the service stopping: the config server passes it on) unwinds like an error: no paid call may start any
    more, the round's jobs are stopped, and the GPU pool's ceiling, the spend and the run lock are settled by their
    finally blocks (which a bare SIGTERM would skip)."""
    def stopped(signum, frame):
        spend.halt(f"stopped by signal {signum}")
        raise SystemExit(f"stopped by signal {signum}")
    signal.signal(signal.SIGTERM, stopped)


def gpu_round(batch, sel, tag, todo, run_id, deadline, spend):
    """The GPU round for `todo`, then the stores re-read: (problems, stopped). Problems: failed jobs, a round stopped at
    the work window or stuck, a round skipped (too little window left, or the budget left covers not one slice: the
    hosted steps still judge what is finished, within their own reservations), listed photos left without a result.
    stopped: paid work has ended (the window, a stop, a lost lock), so no step after it starts."""
    watermarked = {src: load_source(src).watermarked for src in todo}
    try:
        problems = [f"GPU job {name} failed: {why}"
                    for name, why in gpu.run_round(batch, sel.client, tag, todo, watermarked, run_id, deadline, spend)]
    except select.Halted as exc:
        return [f"GPU round {tag} not started: {exc}"], True
    except (gpu.Skipped, select.SpendCapReached) as exc:
        return [f"GPU round {tag} not started: {exc}"], False
    except TimeoutError as exc:                        # its jobs were stopped; what they finished stays
        problems = [f"GPU round {tag} stopped: {exc}"]
    sel.refresh()
    rnd = dict(sel.rounds)[tag]
    problems += [f"{src} {sha[:12]}: no result after the GPU round" for src, shas in todo.items() for sha in shas
                 if sha not in rnd[src].finished | rnd[src].held]
    return problems, False


def hosted(step, sel, spend, deadline):
    """A hosted step over every listing, run a second time for the listings that failed (a passing hiccup): (problems,
    stopped)."""
    failures, stopped = step(sel, spend, deadline)
    if failures and not stopped:
        failures, stopped = step(sel, spend, deadline)
    return [f"{key}: {why}" for key, why in failures], stopped


def run(batch, sel, spend, run_id, deadline, say=print, clock=time.monotonic):
    """The GPU and hosted steps, then the report: (problems, report). A step starts only inside the work window and while
    the budget lasts; the report always runs, so what is chosen is recorded and published."""
    steps = [lambda: gpu_round(batch, sel, sel.tags[0], sel.gpu_todo(), run_id, deadline, spend),
             lambda: hosted(select.judge_all, sel, spend, deadline),
             *([lambda: hosted(select.clean_all, sel, spend, deadline)] if C.CLEAN_ENABLED else []),
             lambda: gpu_round(batch, sel, sel.tags[1], sel.clean_todo(), run_id, deadline, spend),
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
    return problems, result


def process(s3, batch, spend, run_id, deadline, say=print):
    """Clear, pick, work, report and publish (steps 0 to 8), inside the lock: the run's summary (counts), also kept with
    the pending listings and the problems as summary.json under the clean tag. At the end of the work window paid work
    stops (no hosted call or GPU round starts after it)."""
    root = f"{ROOT}{C.CLEAN_TAG}/"
    if left := gpu.clear_leftovers(batch, run_id):
        say(f"==> stopped {len(left)} GPU jobs an earlier run left")
    exclude = publish.read_exclude(publish.EXCLUDE_PATH.read_text())
    found = todo(sheet_listings(s3), choices(s3, root), exclude)
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG)
    say(f"==> standardise process {run_id}: {len(found)} listings without a chosen photo; {C.RUN_TAG} then {C.CLEAN_TAG}")
    closing = threading.Timer(max(0.0, deadline - time.monotonic()), spend.halt, ("the work window ended",))
    closing.daemon = True
    closing.start()
    try:
        problems, result = run(batch, sel, spend, run_id, deadline, say)
    finally:
        closing.cancel()
    try:
        published = publish.publish(s3, root, say)
    except publish.NoChoiceRecords as exc:             # nothing chosen yet in this environment: nothing to publish
        problems.append(str(exc))
        published = {"changed": False}
    for p in problems:
        say(f"   {p}")
    summary = {"run_id": run_id, "listings": len(found), "chosen": len(result["chosen"]), "review": len(result["tracked"]),
               "pending": len(result["pending"]), "problems": len(problems), "published": published,
               "run_spent_usd": round(spend.spent, 4), "month_spent_usd": round(spend.before + spend.spent, 4),
               "budget_usd": spend.budget, "budget_reached": spend.capped}
    kept = {**summary, "pending_listings": result["pending"], "problem_list": problems}
    s3.put_object(Bucket=S3_BUCKET, Key=f"{root}summary.json", Body=json.dumps(kept, sort_keys=True).encode(),
                  ContentType="application/json")
    return summary


def relink():
    """produce --stage republish, so the products link the photos just published (the image stage reads the published
    folder). In this process, never a child: a run the runner stops takes its relink with it, so no produce goes on
    outside the run slot. Its stdout is image-progress noise, dropped as the runner drops it for every produce; its log
    goes to stderr. The pipeline is imported only when a relink is due."""
    from stone_pipeline import produce
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
        return produce.main(["--stage", "republish"])


def reap():
    """After the runner killed the process (it outlived its stop grace, so its own cleanup never ran): stop the GPU jobs
    it left, set the pool's ceiling back and remove its run lock. The runner still holds the run slot, so no other
    process can hold that lock; one rewritten meanwhile (another ETag) is left as it is."""
    retries = Config(retries={"mode": "standard", "max_attempts": 10})
    s3 = boto3.client("s3", region_name=S3_REGION, config=retries)
    left = gpu.clear_leftovers(boto3.client("batch", region_name=S3_REGION, config=retries), "")   # no run of its own
    try:
        _release(s3, s3.head_object(Bucket=S3_BUCKET, Key=LOCK)["ETag"])
    except ClientError as exc:
        if _code(exc) not in _MISSING:
            raise
    print(f"==> cleared what a killed standardise process left: {len(left)} GPU jobs stopped, its lock removed", flush=True)


def unconfigured():
    """What is missing or wrong in the settings: the GPU target, the hosted key, the month's budget, and a work window
    plus its finish that must end before the runner's ceiling."""
    missing = []
    for name in ("GPU_QUEUE", "STANDARDISE_JOBDEF"):          # what gpu.target() reads, each named when missing
        try:
            env.require(name)
        except KeyError as exc:
            missing.append(exc.args[0])
    try:
        judge.fal_key()
    except RuntimeError:
        missing.append("FAL_KEY")
    if C.BUDGET_USD is None:
        missing.append("SCRAPER_STANDARDISE_BUDGET_USD")
    if C.WORK_SECONDS + C.FINISH_SECONDS >= RUN_TIMEOUT_SECONDS:
        missing.append(f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) + FINISH_SECONDS ({C.FINISH_SECONDS}) "
                       f"under SCRAPER_RUN_TIMEOUT_SECONDS ({RUN_TIMEOUT_SECONDS})")
    return missing


def hand_over(summary):
    """The run's summary to the runner (SCRAPER_RUN_SUMMARY), for its run record; nothing when run by hand."""
    path = env.getenv("RUN_SUMMARY")
    if path:
        with open(path, "w") as out:
            json.dump(summary, out, sort_keys=True)


def main() -> int:
    if missing := unconfigured():
        print(f"ERROR: not configured: {', '.join(missing)}; aborting")
        return 2
    deadline = time.monotonic() + C.WORK_SECONDS
    retries = Config(retries={"mode": "standard", "max_attempts": 10})     # a throttled call is retried, never fatal
    s3 = boto3.client("s3", region_name=S3_REGION, config=retries)
    batch = boto3.client("batch", region_name=S3_REGION, config=retries)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    spend = month_spend(s3, run_id)
    stop_on_signal(spend)                              # before the lock: a stop at any point releases it
    try:
        with exclusive(s3, {"run_id": run_id, "tags": [C.RUN_TAG, C.CLEAN_TAG]},
                       on_lost=lambda: spend.halt("the run lock was taken over")):
            summary = process(s3, batch, spend, run_id, deadline)
    except RunActive as exc:
        print(f"ERROR: {exc}; not started")
        return 4
    print(f"==> spent ${spend.spent:.2f} this run, ${spend.before + spend.spent:.2f} of ${spend.budget:.2f} this month"
          + ("; the month's budget is reached: the rest waits for next month or a higher budget" if spend.capped else ""),
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
    hand_over(summary)
    return rc


if __name__ == "__main__":
    sys.exit(main())
