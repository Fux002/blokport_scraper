"""A standardise run, end to end, for one environment and a set of listings: each listing ends with one good photo
(published by deploy.standardise_publish) or on the tracked list with the reason. In order:
  1. GPU: the listings' first photos the standardiser has not run yet (deploy.standardise_batch)
  2. judge: each listing's results in its own order until one passes
  3. clean: a hosted edit of one fixable photo of each listing still without a pass
  4. GPU: the cleaned copies, through every step a scraped photo takes
  5. judge: the cleaned copies' results
  6. report: one choice record per listing (what publish reads) + tracked.csv
Every step stores what it did, so a run that stops is run again and goes on where it stopped, paying nothing twice.
Env: RUN_TAG, CLEAN_TAG (the scraped and the cleaned run), SRC (space-separated sources), LISTINGS_KEY (optional:
an S3 JSON list of listing keys), FAL_KEY, SCRAPER_GPU_QUEUE, SCRAPER_STANDARDISE_JOBDEF.
One run at a time per environment: a run holds <env>/standardise-test/run.lock (written only if absent) and removes it
when it ends; a second run refuses to start (its GPU rounds would change the shared GPU pool's ceiling under the first).
A lock left by a run that was killed is removed by hand after checking no run is active.
Exit 0 done; 1 some listings or photos failed (run again); 2 not configured; 3 the hosted spend cap was reached;
4 another run is active.
"""

from __future__ import annotations

import json
import os
import signal
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from deploy import standardise_batch as gpu
from deploy import standardise_select as select
from deploy.standardise_source import sheet_listings
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.config.sources import load_source


LOCK = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/run.lock"


class RunActive(RuntimeError):
    pass


@contextmanager
def exclusive(s3, owner):
    """Hold the environment's run lock for the block; RunActive when another run holds it."""
    try:
        s3.put_object(Bucket=S3_BUCKET, Key=LOCK, Body=json.dumps(owner).encode(), IfNoneMatch="*")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise RunActive(f"another standardise run holds s3://{S3_BUCKET}/{LOCK}") from exc
        raise
    try:
        yield
    finally:
        s3.delete_object(Bucket=S3_BUCKET, Key=LOCK)


def listings(s3, sources, wanted_key):
    """This environment's listings of the sources, limited to the stored list when one is given. ValueError when a
    source has none (a mistyped source, or a list that names none of its listings): a run of nothing is no run."""
    keep = set(json.loads(s3.get_object(Bucket=S3_BUCKET, Key=wanted_key)["Body"].read())) if wanted_key else None
    found = [l for l in sheet_listings(s3) if l["source"] in sources and (keep is None or l["key"] in keep)]
    if empty := sorted(sources - {l["source"] for l in found}):
        raise ValueError(f"no listings for {', '.join(empty)} in this environment's product sheet")
    return found


def stop_on_signal(spend):
    """SIGTERM (a stopped task or a closed terminal) unwinds like an error: no hosted call may start any more, the round's
    jobs are stopped, and the GPU pool's ceiling and the run lock are restored by their finally blocks (which a bare
    SIGTERM would skip)."""
    def stopped(signum, frame):
        spend.halt()
        raise SystemExit(f"stopped by signal {signum}")
    signal.signal(signal.SIGTERM, stopped)


def gpu_round(batch, sel, tag, todo, run_id):
    """The GPU round for `todo`, then the stores re-read: [problems] (failed jobs, listed photos left without a result)."""
    watermarked = {src: load_source(src).watermarked for src in todo}
    problems = [f"GPU job {name} failed: {why}" for name, why in gpu.run_round(batch, sel.client, tag, todo, watermarked, run_id)]
    sel.refresh()
    rnd = dict(sel.rounds)[tag]
    problems += [f"{src} {sha[:12]}: no result after the GPU round" for src, shas in todo.items() for sha in shas
                 if sha not in rnd[src].finished | rnd[src].held]
    return problems


def hosted(step, sel, spend):
    """A hosted step over every listing, run a second time for the listings that failed (a passing hiccup): (problems,
    cap reached)."""
    failures, capped = step(sel, spend)
    if failures and not capped:
        failures, capped = step(sel, spend)
    return [f"{key}: {why}" for key, why in failures], capped


def run(batch, sel, spend, run_id, say=print):
    """The six steps; (problems, cap reached). Stops at the cap: what is stored stays, the next run goes on."""
    problems = gpu_round(batch, sel, sel.tags[0], sel.gpu_todo(), run_id)
    for step in (select.judge_all, select.clean_all) if C.CLEAN_ENABLED else (select.judge_all,):
        found, capped = hosted(step, sel, spend)
        problems += found
        if capped:
            return problems, True
    problems += gpu_round(batch, sel, sel.tags[1], sel.clean_todo(), run_id)
    found, capped = hosted(select.judge_all, sel, spend)
    problems += found
    if capped:
        return problems, True
    result = select.report(sel)
    by_tag = {tag: sum(c["tag"] == tag for c in result["chosen"]) for tag in sel.tags}
    say(f"==> {len(result['chosen'])} of {len(sel.listings)} listings with a good photo {by_tag}; "
        f"{len(result['tracked'])} on the tracked list (s3://{S3_BUCKET}/{sel.root}tracked.csv)")
    problems += [f"{key}: not fully judged" for key in result["pending"]]
    return problems, False


def main() -> int:
    run_tag, clean_tag = os.environ.get("RUN_TAG", "").strip(), os.environ.get("CLEAN_TAG", "").strip()
    sources = set(os.environ.get("SRC", "").split())
    if not sources or not run_tag or not clean_tag or run_tag == clean_tag:
        print("ERROR: RUN_TAG, CLEAN_TAG (a different tag) and SRC are required; aborting")
        return 2
    retries = Config(retries={"mode": "standard", "max_attempts": 10})     # a throttled call is retried, never fatal
    s3 = boto3.client("s3", region_name=S3_REGION, config=retries)
    batch = boto3.client("batch", region_name=S3_REGION, config=retries)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    owner = {"run_id": run_id, "tags": [run_tag, clean_tag], "sources": sorted(sources)}
    try:
        found = listings(s3, sources, os.environ.get("LISTINGS_KEY", "").strip())
    except ValueError as exc:
        print(f"ERROR: {exc}; aborting")
        return 2
    spend = select.Spend(C.SPEND_CAP_USD)
    stop_on_signal(spend)                              # before the lock: a stop at any point releases it
    try:
        with exclusive(s3, owner):
            sel = select.Selection(s3, found, run_tag, clean_tag)
            print(f"==> standardise run {run_id}: {len(sel.listings)} listings of {', '.join(sel.sources)}; "
                  f"{run_tag} then {clean_tag}", flush=True)
            problems, capped = run(batch, sel, spend, run_id)
    except RunActive as exc:
        print(f"ERROR: {exc}; not started")
        return 4
    print(f"==> hosted spend ${spend.spent:.2f}", flush=True)
    for p in problems:
        print(f"   {p}", flush=True)
    if capped:
        print(f"==> STOPPED at the hosted spend cap (${C.SPEND_CAP_USD:.2f}); run again to go on", flush=True)
        return 3
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
