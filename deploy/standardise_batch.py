"""One GPU round of a standardise run on AWS Batch: the listed photos of each source, in slices of RUN_SLICE, each slice a
retouch job then a standardise job that starts when the retouch ended (STANDARDISE_ROUND), or one close-only job
(CLOSE_ROUND: deploy.standardise_close), every job reading the slice's own fixed photo list (ONLY_KEY). Both kinds share
one path: sized to the month's budget, reserved and settled, the GPU pool's ceiling raised for the round and always set
back, a round that fails, times out or is stopped stopping its jobs. Queue and job definition come from the
environment (SCRAPER_GPU_QUEUE, SCRAPER_STANDARDISE_JOBDEF), never named here."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError

from deploy.standardise_publish import pool_map
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, STANDARDISE as C
from stone_pipeline.core import env

_DONE = ("SUCCEEDED", "FAILED")
# Each job runs once, as its reservation counts (worst_seconds). A job that stops on purpose exits 1 (its photos are left
# to the next run, recorded in S3); a second attempt would only repeat that unpaid for. Set on every submission, so the
# job definition's own retry setting can never change what a round costs.
ONE_ATTEMPT = {"attempts": 1}


def target():
    """(queue, job definition) of this environment's standardise jobs; fails loud when either is not configured."""
    return env.require("GPU_QUEUE"), env.require("STANDARDISE_JOBDEF")


def pool(batch, queue):
    """The compute environment behind the queue (the GPU pool whose ceiling a run raises)."""
    order = batch.describe_job_queues(jobQueues=[queue])["jobQueues"][0]["computeEnvironmentOrder"]
    return order[0]["computeEnvironment"]


def _ceiling(batch, ce, vcpus):
    batch.update_compute_environment(computeEnvironment=ce, computeResources={"maxvCpus": vcpus})


def idle_vcpus():
    """The pool's idle ceiling (IDLE_GPU_VCPUS, from the environment Terraform sets); fails loud when this host was given
    none, so a run never leaves the pool at a guessed ceiling."""
    if C.IDLE_GPU_VCPUS is None:
        raise KeyError("SCRAPER_STANDARDISE_IDLE_GPU_VCPUS")
    return C.IDLE_GPU_VCPUS


@contextmanager
def raised(batch, ce):
    """The pool's ceiling at RUN_GPU_VCPUS for the block, back at the idle ceiling after, whatever happened in it. The idle
    ceiling is read first: a host without one fails before it raises the pool."""
    idle = idle_vcpus()
    _ceiling(batch, ce, C.RUN_GPU_VCPUS)
    try:
        yield
    finally:
        _ceiling(batch, ce, idle)


def slices(todo):
    """[(source, n, shas)]: every source's listed photos in chunks of RUN_SLICE. Each slice's retouch and standardise job
    read the same fixed list, so a product sheet rewritten between the two can never shift which photos a job gets."""
    return [(src, n, shas[i:i + C.RUN_SLICE]) for src, shas in sorted(todo.items())
            for n, i in enumerate(range(0, len(shas), C.RUN_SLICE))]


def submit(batch, s3, queue, jobdef, tag, todo, run_id, ids):
    """Store each slice's photo list (the run's own files, run_id) and submit its two jobs, appending each job id to ids as
    it is submitted (so a submit that fails part way still knows its jobs). todo = {source: shas}."""
    for src, n, shas in slices(todo):
        only = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/gpu-todo-{run_id}-{src}-{n}.json"
        s3.put_object(Bucket=S3_BUCKET, Key=only, Body=json.dumps(shas).encode(), ContentType="application/json")
        common = [{"name": k, "value": str(v)} for k, v in (("SRC", src), ("RUN_TAG", tag), ("ONLY_KEY", only))]
        name = f"{src}-{tag}-{n}"
        retouch = batch.submit_job(jobName=f"{C.JOB_PREFIX}{run_id}-retouch-{name}", jobQueue=queue, jobDefinition=jobdef,
                                   timeout={"attemptDurationSeconds": C.RETOUCH_TIMEOUT}, retryStrategy=ONE_ATTEMPT,
                                   containerOverrides={"environment": [{"name": "RUN_MODE", "value": "standardise-retouch"}] + common})["jobId"]
        ids.append(retouch)
        standardise = batch.submit_job(jobName=f"{C.JOB_PREFIX}{run_id}-standardise-{name}", jobQueue=queue, jobDefinition=jobdef,
                                       dependsOn=[{"jobId": retouch}], timeout={"attemptDurationSeconds": C.STANDARDISE_TIMEOUT},
                                       retryStrategy=ONE_ATTEMPT,
                                       containerOverrides={"environment": [{"name": "RUN_MODE", "value": "standardise"}]
                                                           + common})["jobId"]
        ids.append(standardise)
    return ids


def submit_close(batch, s3, queue, jobdef, tag, todo, run_id, ids):
    """Store each slice's photo list (the run's own files, run_id) and submit its close-only job (deploy.standardise_close:
    the chosen photos' close-ups made again), appending each job id to ids as it is submitted. todo = {source: shas of its
    chosen photos}; tag: the clean tag, whose choice records the job reads."""
    for src, n, shas in slices(todo):
        only = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/gpu-close-{run_id}-{src}-{n}.json"
        s3.put_object(Bucket=S3_BUCKET, Key=only, Body=json.dumps(shas).encode(), ContentType="application/json")
        environment = (("RUN_MODE", "standardise-close"), ("SRC", src), ("CLEAN_TAG", tag), ("ONLY_KEY", only))
        ids.append(batch.submit_job(jobName=f"{C.JOB_PREFIX}{run_id}-close-{src}-{tag}-{n}", jobQueue=queue, jobDefinition=jobdef,
                                    timeout={"attemptDurationSeconds": C.STANDARDISE_TIMEOUT}, retryStrategy=ONE_ATTEMPT,
                                    containerOverrides={"environment": [{"name": k, "value": v} for k, v in environment]})["jobId"])
    return ids


@dataclass(frozen=True)
class Work:
    """What each slice of a round runs: `jobs` jobs, one after another (one GPU at a time), within `seconds` together,
    submitted by `submit`; `judged`: whether the judge then looks at each photo's result (a close-up round's are not)."""
    jobs: int
    seconds: int
    submit: Callable
    judged: bool


STANDARDISE_ROUND = Work(2, C.RETOUCH_TIMEOUT + C.STANDARDISE_TIMEOUT, submit, True)
CLOSE_ROUND = Work(1, C.STANDARDISE_TIMEOUT, submit_close, False)


def wait(batch, ids, until, sleep=time.sleep, clock=time.monotonic):
    """Block until every job has ended: [(job name, why)] of the jobs that failed. TimeoutError once `until` (on clock)
    has passed (jobs stuck waiting for GPUs never end on their own)."""
    while True:
        jobs = [j for i in range(0, len(ids), 100) for j in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]]
        if all(j["status"] in _DONE for j in jobs):
            return [(j["jobName"], j.get("statusReason", "")) for j in jobs if j["status"] == "FAILED"]
        if clock() > until:
            raise TimeoutError("GPU jobs not done by the round's deadline")
        sleep(C.RUN_POLL_SECONDS)


def stop(batch, ids, why):
    """Stop every job (Batch ignores it for a job that has ended), all at once (a round has hundreds; a stop has seconds),
    each on its own: one that cannot be stopped is reported and the others are still stopped (a run that fails or is
    stopped leaves no GPU work behind)."""
    def one(job_id):
        try:
            batch.terminate_job(jobId=job_id, reason=why[:250])
        except (BotoCoreError, ClientError) as exc:
            print(f"   could not stop GPU job {job_id}: {exc}", flush=True)
    pool_map(C.STORE_WORKERS, one, ids)


def wait_limit(n_slices):
    """How long a round may take: RUN_MAX_WAIT_SECONDS per wave of slices that run at once."""
    at_once = max(1, C.RUN_GPU_VCPUS // C.GPU_JOB_VCPUS)
    return C.RUN_MAX_WAIT_SECONDS * -(-n_slices // at_once)


class Skipped(RuntimeError):
    """A round not started: too little of the work window is left for one to be worth its boot."""


def gpu_usd(seconds):
    """What this many seconds of GPU job time cost (a job's vCPUs at GPU_VCPU_HOUR_USD)."""
    return seconds / 3600 * C.GPU_JOB_VCPUS * C.GPU_VCPU_HOUR_USD


def worst_seconds(n_slices, window, work=STANDARDISE_ROUND):
    """The most GPU-seconds a round of n slices can be charged: its running time, one GPU per slice at a time (its jobs
    one after another, within work.seconds), never past the window and at most RUN_GPU_VCPUS worth at once; plus the
    allowance of every job that can start, which is each of the work's jobs per slice. ran_usd charges exactly these
    parts, so a round never settles above what it reserved."""
    at_once = max(1, C.RUN_GPU_VCPUS // C.GPU_JOB_VCPUS)
    running = min(n_slices * min(work.seconds, window), at_once * window)
    return running + work.jobs * n_slices * C.GPU_JOB_ALLOWANCE_SECONDS


def price(todo, window, work=STANDARDISE_ROUND):
    """The most a round of `todo` ({source: shas}) takes of the process budget, its results judged: the GPU's worst case
    (worst_seconds) and, when the judge looks at its results (work.judged), one verdict per photo. The one price of GPU
    work: a round is sized by it (fitting) and the clean step admits by it (deploy.standardise_run.admit), so no round
    starts work whose results the budget cannot then judge."""
    photos = sum(len(shas) for shas in todo.values())
    return gpu_usd(worst_seconds(len(slices(todo)), window, work)) + (photos * C.JUDGE_PRICE_USD if work.judged else 0.0)


def fitting(plan, window, left, work=STANDARDISE_ROUND):
    """The most of the round's slices (`plan`, slices()) whose price the budget left covers (it only grows with the
    slices)."""
    low, high = 0, len(plan)
    while low < high:
        mid = (low + high + 1) // 2
        if price(_first(plan[:mid]), window, work) <= left:
            low = mid
        else:
            high = mid - 1
    return low


def _first(plan):
    """{source: shas} of the slices in `plan`, so the round's own slicing gives back exactly these."""
    todo = {}
    for src, _, shas in plan:
        todo.setdefault(src, []).extend(shas)
    return todo


def _jobs(batch, ids):
    return [j for i in range(0, len(ids), 100) for j in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]]


def stopped_jobs(batch, ids, sleep=time.sleep):
    """The round's jobs once they have all ended (a stopped job takes a moment), asked every STOP_POLL_SECONDS for at
    most STOP_WAIT_SECONDS; then as they are."""
    for _ in range(C.STOP_WAIT_SECONDS // C.STOP_POLL_SECONDS):
        jobs = _jobs(batch, ids)
        if all(j["status"] in _DONE for j in jobs):
            return jobs
        sleep(C.STOP_POLL_SECONDS)
    return _jobs(batch, ids)


def ran_usd(batch, ids, reserved, now=time.time, sleep=time.sleep):
    """What the round's jobs cost: each started job's running time (startedAt to stoppedAt, or to now for one still not
    stopped) plus its allowance; a job that never started cost nothing. The reserved price when Batch cannot say
    (counted high, never low)."""
    try:
        jobs = stopped_jobs(batch, ids, sleep)
    except (BotoCoreError, ClientError) as exc:
        print(f"   WARNING: GPU job times unreadable ({exc}); the round counts its reserved ${reserved:.2f}", flush=True)
        return reserved
    started = [j for j in jobs if j.get("startedAt") is not None]
    ran_ms = sum((j["stoppedAt"] if j.get("stoppedAt") is not None else now() * 1000) - j["startedAt"] for j in started)
    return gpu_usd(ran_ms / 1000 + len(started) * C.GPU_JOB_ALLOWANCE_SECONDS)


def run_round(batch, s3, tag, todo, run_id, deadline, spend, work=STANDARDISE_ROUND, clock=time.monotonic,
              now=time.time, sleep=time.sleep):
    """Every listed photo through the round's work under `tag` (STANDARDISE_ROUND: retouch and standardise; CLOSE_ROUND:
    the close-up made again): [(job name, why)] of failed jobs. No jobs, no ceiling change, when nothing is listed. Skipped when less than GPU_MIN_WINDOW_SECONDS of the work window (on `clock`)
    is left. The round is sized to the month's budget left: as many whole slices as their price fits (price: the GPU's
    worst case and the judging of their results), the rest waits (SpendCapReached when not one fits: nothing is
    submitted); the GPU's worst case is reserved first (each verdict reserves its own share when it is bought) and
    settled to what the jobs ran, however the round ends. It waits until the deadline at most: then its jobs are stopped
    (TimeoutError), every photo they finished stays, the next run goes on."""
    if not any(todo.values()):
        return []
    started = clock()                                  # the reservation's window and the wait's deadline both count from here
    window = max(0.0, deadline - started)
    if window < C.GPU_MIN_WINDOW_SECONDS:
        raise Skipped(f"only {window:.0f}s of the work window left (a round needs {C.GPU_MIN_WINDOW_SECONDS}s)")
    queue, jobdef = target()
    plan = slices(todo)
    fit = fitting(plan, window, spend.left(), work)
    if not fit:                                        # not one slice and its judging fits: nothing is submitted
        raise spend.refuse()
    todo = _first(plan[:fit])
    reserved = gpu_usd(worst_seconds(len(slices(todo)), window, work))
    spend.reserve(reserved)
    ids = []
    try:
        with raised(batch, pool(batch, queue)):
            try:
                work.submit(batch, s3, queue, jobdef, tag, todo, run_id, ids)
                return wait(batch, ids, started + min(wait_limit(len(slices(todo))), window), clock=clock)
            except BaseException as exc:
                stop(batch, ids, f"standardise run {run_id} stopped: {type(exc).__name__}")
                raise
    finally:
        spend.settle(reserved, ran_usd(batch, ids, reserved, now, sleep))


def _listed(batch, queue):
    """The process's jobs on the queue (named JOB_PREFIX...), every status, page by page."""
    ask = {"jobQueue": queue, "filters": [{"name": "JOB_NAME", "values": [f"{C.JOB_PREFIX}*"]}]}
    while True:
        page = batch.list_jobs(**ask)
        yield from page.get("jobSummaryList", [])
        if not page.get("nextToken"):
            return
        ask["nextToken"] = page["nextToken"]


def _active(batch, run_id):
    """[(job id, whether it is run_id's)] of the process's jobs on the queue that have not ended."""
    queue, _ = target()
    return [(j["jobId"], j["jobName"].startswith(f"{C.JOB_PREFIX}{run_id}-")) for j in _listed(batch, queue)
            if j["status"] not in _DONE]


def set_idle(batch):
    """The GPU pool's ceiling back to idle."""
    idle = idle_vcpus()
    queue, _ = target()
    _ceiling(batch, pool(batch, queue), idle)


def clear_leftovers(batch, run_id):
    """Stop the jobs an earlier run left still active (its process was killed before it could) and set the GPU pool's
    ceiling back to idle: [the stopped job ids]. Called while holding the run lock, so no other process is running and
    every active job of the process that is not this run's was left."""
    left = [job_id for job_id, own in _active(batch, run_id) if not own]
    stop(batch, left, f"left by an earlier standardise run; stopped by {run_id}")
    set_idle(batch)
    return left


def stop_run(batch, run_id):
    """Stop run_id's own jobs that are still active, and no other's: [their ids]."""
    own = [job_id for job_id, mine in _active(batch, run_id) if mine]
    stop(batch, own, f"standardise run {run_id} was killed")
    return own
