"""The standardised-photo process of an environment (STANDARDISE_PROCESS.md): every listing of the product sheet without
a valid chosen photo ends with one good photo, published and linked, or on the review list with the reason. The
runner's `photos` stage, started after each produce on the owner's button. In order:
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
GPU and hosted steps work at most STANDARDISE.WORK_SECONDS; hosted calls stop before the month's spend would pass
STANDARDISE.BUDGET_USD (all runs of the month together, kept at <env>/standardise-test/spend/<YYYY-MM>.json).
One process at a time per environment: it holds <env>/standardise-test/run.lock (written only if absent, taken over only
when older than LOCK_STALE_SECONDS: its run was killed) and removes it when publish has ended.
Env: SCRAPER_GPU_QUEUE, SCRAPER_STANDARDISE_JOBDEF, FAL_KEY, SCRAPER_STANDARDISE_BUDGET_USD (all required).
Exit 0 done; 1 some listings or photos failed or the work window ended (the next run goes on); 2 not configured;
3 the month's budget was reached; 4 another process is running; a failed relink exits with the produce's code.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from deploy import standardise_batch as gpu
from deploy import standardise_publish as publish
from deploy import standardise_select as select
from deploy.standardise_source import sheet_listings
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.config.sources import load_source
from stone_pipeline.core import env
from stone_pipeline.ledger import writethrough
from stone_pipeline.standardise import judge

ROOT = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/"
LOCK = f"{ROOT}run.lock"
_TAKEN = ("PreconditionFailed", "ConditionalRequestConflict")


class RunActive(RuntimeError):
    pass


def _taken(exc):
    return exc.response.get("Error", {}).get("Code") in _TAKEN


@contextmanager
def exclusive(s3, owner, now=None):
    """Hold the environment's run lock for the block; RunActive when another process holds it."""
    try:
        s3.put_object(Bucket=S3_BUCKET, Key=LOCK, Body=json.dumps(owner).encode(), IfNoneMatch="*")
    except ClientError as exc:
        if not _taken(exc):
            raise
        _take_over(s3, owner, now or datetime.now(timezone.utc))
    try:
        yield
    finally:
        s3.delete_object(Bucket=S3_BUCKET, Key=LOCK)


def _take_over(s3, owner, now):
    """The lock is held: taken over only when older than LOCK_STALE_SECONDS (the runner ends every run long before, so its
    run was killed), in one write conditional on the lock seen, so of two processes taking it over one does. RunActive
    otherwise."""
    held = s3.head_object(Bucket=S3_BUCKET, Key=LOCK)
    since = held["LastModified"]
    if (now - since).total_seconds() < C.LOCK_STALE_SECONDS:
        raise RunActive(f"another standardise process holds s3://{S3_BUCKET}/{LOCK} (since {since:%Y-%m-%d %H:%M} UTC)")
    try:
        s3.put_object(Bucket=S3_BUCKET, Key=LOCK, Body=json.dumps(owner).encode(), IfMatch=held["ETag"])
    except ClientError as exc:
        if _taken(exc):
            raise RunActive(f"another standardise process took over s3://{S3_BUCKET}/{LOCK} first") from exc
        raise
    print(f"==> took over the lock of a killed run (written {since:%Y-%m-%d %H:%M} UTC)", flush=True)


# --- the month's budget -----------------------------------------------------------------------------------------------
def spend_key(month):
    return f"{ROOT}{C.SPEND_DIR}/{month}.json"


def month_spend(s3, run_id):
    """This run's Spend against the month's budget: what the month's other runs spent, and this run's total kept in the
    month's file after every change. The month is the run's start (UTC), from its id (YYYYMMDDTHHMMSSZ)."""
    key = spend_key(f"{run_id[:4]}-{run_id[4:6]}")
    try:
        runs = json.loads(s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())["runs"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchKey":
            raise
        runs = {}                                      # the month's first run
    others = {r: usd for r, usd in runs.items() if r != run_id}

    def save(total):
        body = json.dumps({"runs": {**others, run_id: round(total, 6)}}, sort_keys=True).encode()
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType="application/json")
    return select.Spend(C.BUDGET_USD, sum(others.values()), save)


# --- what a run works on ----------------------------------------------------------------------------------------------
def choices(s3, root):
    """{listing key: its choice record} under the clean tag's root."""
    keys = [o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=publish.chosen_prefix(root))
            for o in page.get("Contents", [])]
    with ThreadPoolExecutor(publish.WORKERS) as pool:
        records = list(pool.map(lambda k: json.loads(s3.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read()), keys))
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
    """SIGTERM (a stopped task) unwinds like an error: no hosted call may start any more, the round's jobs are stopped,
    and the GPU pool's ceiling and the run lock are restored by their finally blocks (which a bare SIGTERM would skip)."""
    def stopped(signum, frame):
        spend.halt()
        raise SystemExit(f"stopped by signal {signum}")
    signal.signal(signal.SIGTERM, stopped)


def gpu_round(batch, sel, tag, todo, run_id, deadline):
    """The GPU round for `todo`, then the stores re-read: [problems] (failed jobs, a round stopped at the work window or
    stuck, listed photos left without a result)."""
    watermarked = {src: load_source(src).watermarked for src in todo}
    try:
        problems = [f"GPU job {name} failed: {why}" for name, why in gpu.run_round(batch, sel.client, tag, todo, watermarked, run_id, deadline)]
    except TimeoutError as exc:                        # its jobs were stopped; what they finished stays
        problems = [f"GPU round {tag} stopped: {exc}"]
    sel.refresh()
    rnd = dict(sel.rounds)[tag]
    problems += [f"{src} {sha[:12]}: no result after the GPU round" for src, shas in todo.items() for sha in shas
                 if sha not in rnd[src].finished | rnd[src].held]
    return problems


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
    steps = [lambda: (gpu_round(batch, sel, sel.tags[0], sel.gpu_todo(), run_id, deadline), False),
             lambda: hosted(select.judge_all, sel, spend, deadline),
             *([lambda: hosted(select.clean_all, sel, spend, deadline)] if C.CLEAN_ENABLED else []),
             lambda: (gpu_round(batch, sel, sel.tags[1], sel.clean_todo(), run_id, deadline), False),
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
    """Pick, work, report and publish (steps 1 to 8), inside the lock: the run's summary, also kept as summary.json under
    the clean tag."""
    root = f"{ROOT}{C.CLEAN_TAG}/"
    exclude = publish.read_exclude(publish.EXCLUDE_PATH.read_text())
    found = todo(sheet_listings(s3), choices(s3, root), exclude)
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG)
    say(f"==> standardise process {run_id}: {len(found)} listings without a chosen photo; {C.RUN_TAG} then {C.CLEAN_TAG}")
    problems, result = run(batch, sel, spend, run_id, deadline, say)
    try:
        published = publish.publish(s3, root, say)
    except ValueError as exc:                          # nothing chosen yet in this environment: nothing to publish
        problems.append(str(exc))
        published = {"changed": False}
    for p in problems:
        say(f"   {p}")
    summary = {"run_id": run_id, "listings": len(found), "chosen": len(result["chosen"]), "review": len(result["tracked"]),
               "pending": result["pending"], "problems": len(problems), "published": published,
               "run_spent_usd": round(spend.spent, 4), "month_spent_usd": round(spend.before + spend.spent, 4),
               "budget_usd": spend.budget, "budget_reached": spend.capped}
    s3.put_object(Bucket=S3_BUCKET, Key=f"{root}summary.json", Body=json.dumps(summary, sort_keys=True).encode(),
                  ContentType="application/json")
    return summary


def relink():
    """produce --stage republish, so the products link the photos just published (the image stage reads the published
    folder). In this process, never a child: a run the runner stops at its ceiling takes its relink with it, so no produce
    goes on outside the run slot. Its stdout is image-progress noise, dropped as the runner drops it for every produce;
    its log goes to stderr. The pipeline is imported only when a relink is due."""
    from stone_pipeline import produce
    with open(os.devnull, "w") as quiet, redirect_stdout(quiet):
        return produce.main(["--stage", "republish"])


def unconfigured():
    """The required settings that are missing (the GPU target, the hosted key, the month's budget)."""
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
    return missing


def main() -> int:
    if missing := unconfigured():
        print(f"ERROR: not configured: {', '.join(missing)}; aborting")
        return 2
    deadline = time.monotonic() + C.WORK_SECONDS
    retries = Config(retries={"mode": "standard", "max_attempts": 10})     # a throttled call is retried, never fatal
    s3 = boto3.client("s3", region_name=S3_REGION, config=retries)
    batch = boto3.client("batch", region_name=S3_REGION, config=retries)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Read before the lock, so a stop at any point releases it: a run that held the lock wrote its last spend before its
    # publish, which ends long before it releases the lock, so what is read here is final.
    spend = month_spend(s3, run_id)
    stop_on_signal(spend)
    try:
        with exclusive(s3, {"run_id": run_id, "tags": [C.RUN_TAG, C.CLEAN_TAG]}):
            summary = process(s3, batch, spend, run_id, deadline)
    except RunActive as exc:
        print(f"ERROR: {exc}; not started")
        return 4
    print(f"==> hosted spend ${spend.spent:.2f} this run, ${spend.before + spend.spent:.2f} of ${spend.budget:.2f} this month",
          flush=True)
    rc = 3 if spend.capped else 1 if summary["problems"] else 0
    if spend.capped:
        print("==> STOPPED at the month's budget: the rest waits for next month or a higher SCRAPER_STANDARDISE_BUDGET_USD",
              flush=True)
    if summary["published"]["changed"]:
        if not writethrough.enabled():
            print("==> published; no ledger here, the next produce links the new photos", flush=True)
        elif (linked := relink()) != 0:
            print(f"==> FAILED to link the new photos: the republish exited {linked}", flush=True)
            return linked
        else:
            print("==> the new photos are linked (republish done)", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
