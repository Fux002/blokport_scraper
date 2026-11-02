"""One GPU round of a standardise run on AWS Batch: the listed photos of each source, in slices of RUN_SLICE, each slice a
retouch job then a standardise job that starts when the retouch ended, both reading the slice's own fixed photo list
(ONLY_KEY). The GPU pool's ceiling is raised for the round and always set back; a round that fails, times out or is
stopped stops its jobs. Queue and job definition come from the
environment (SCRAPER_GPU_QUEUE, SCRAPER_STANDARDISE_JOBDEF), never named here."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

from botocore.exceptions import BotoCoreError, ClientError

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, STANDARDISE as C
from stone_pipeline.core import env

_DONE = ("SUCCEEDED", "FAILED")


def target():
    """(queue, job definition) of this environment's standardise jobs; fails loud when either is not configured."""
    return env.require("GPU_QUEUE"), env.require("STANDARDISE_JOBDEF")


def pool(batch, queue):
    """The compute environment behind the queue (the GPU pool whose ceiling a run raises)."""
    order = batch.describe_job_queues(jobQueues=[queue])["jobQueues"][0]["computeEnvironmentOrder"]
    return order[0]["computeEnvironment"]


def _ceiling(batch, ce, vcpus):
    batch.update_compute_environment(computeEnvironment=ce, computeResources={"maxvCpus": vcpus})


@contextmanager
def raised(batch, ce):
    """The pool's ceiling at RUN_GPU_VCPUS for the block, back at IDLE_GPU_VCPUS after, whatever happened in it."""
    _ceiling(batch, ce, C.RUN_GPU_VCPUS)
    try:
        yield
    finally:
        _ceiling(batch, ce, C.IDLE_GPU_VCPUS)


def slices(todo):
    """[(source, n, shas)]: every source's listed photos in chunks of RUN_SLICE. Each slice's retouch and standardise job
    read the same fixed list, so a product sheet rewritten between the two can never shift which photos a job gets."""
    return [(src, n, shas[i:i + C.RUN_SLICE]) for src, shas in sorted(todo.items())
            for n, i in enumerate(range(0, len(shas), C.RUN_SLICE))]


def submit(batch, s3, queue, jobdef, tag, todo, watermarked, run_id, ids):
    """Store each slice's photo list (the run's own files, run_id) and submit its two jobs, appending each job id to ids as
    it is submitted (so a submit that fails part way still knows its jobs). todo = {source: shas}; watermarked =
    {source: bool}."""
    for src, n, shas in slices(todo):
        only = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/gpu-todo-{run_id}-{src}-{n}.json"
        s3.put_object(Bucket=S3_BUCKET, Key=only, Body=json.dumps(shas).encode(), ContentType="application/json")
        common = [{"name": k, "value": str(v)} for k, v in (("SRC", src), ("RUN_TAG", tag), ("ONLY_KEY", only))]
        name = f"{src}-{tag}-{n}"
        retouch = batch.submit_job(jobName=f"{C.JOB_PREFIX}{run_id}-retouch-{name}", jobQueue=queue, jobDefinition=jobdef,
                                   timeout={"attemptDurationSeconds": C.RETOUCH_TIMEOUT},
                                   containerOverrides={"environment": [{"name": "RUN_MODE", "value": "standardise-retouch"}] + common})["jobId"]
        ids.append(retouch)
        standardise = batch.submit_job(jobName=f"{C.JOB_PREFIX}{run_id}-standardise-{name}", jobQueue=queue, jobDefinition=jobdef,
                                       dependsOn=[{"jobId": retouch}], timeout={"attemptDurationSeconds": C.STANDARDISE_TIMEOUT},
                                       containerOverrides={"environment": [{"name": "RUN_MODE", "value": "standardise"},
                                                                           {"name": "WATERMARKED", "value": str(watermarked[src]).lower()}]
                                                           + common})["jobId"]
        ids.append(standardise)
    return ids


def wait(batch, ids, limit, sleep=time.sleep, clock=time.monotonic):
    """Block until every job has ended: [(job name, why)] of the jobs that failed. TimeoutError after limit seconds
    (jobs stuck waiting for GPUs never end on their own)."""
    deadline = clock() + limit
    while True:
        jobs = [j for i in range(0, len(ids), 100) for j in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]]
        if all(j["status"] in _DONE for j in jobs):
            return [(j["jobName"], j.get("statusReason", "")) for j in jobs if j["status"] == "FAILED"]
        if clock() > deadline:
            raise TimeoutError(f"GPU jobs not done after {limit}s")
        sleep(C.RUN_POLL_SECONDS)


def stop(batch, ids, why):
    """Stop every job of the round (Batch ignores it for a job that has ended), each on its own: one that cannot be
    stopped is reported and the others are still stopped (a run that fails or is stopped leaves no GPU work behind)."""
    for job_id in ids:
        try:
            batch.terminate_job(jobId=job_id, reason=why[:250])
        except (BotoCoreError, ClientError) as exc:
            print(f"   could not stop GPU job {job_id}: {exc}", flush=True)


def wait_limit(n_slices):
    """How long a round may take: RUN_MAX_WAIT_SECONDS per wave of slices that run at once."""
    at_once = max(1, C.RUN_GPU_VCPUS // C.GPU_JOB_VCPUS)
    return C.RUN_MAX_WAIT_SECONDS * -(-n_slices // at_once)


class Skipped(RuntimeError):
    """A round not started: too little of the work window is left for one to be worth its boot."""


def gpu_usd(seconds):
    """What this many seconds of GPU job time cost (a job's vCPUs at GPU_VCPU_HOUR_USD)."""
    return seconds / 3600 * C.GPU_JOB_VCPUS * C.GPU_VCPU_HOUR_USD


def worst_seconds(n_slices, window):
    """The most GPU-seconds a round of n slices can be charged: its running time, one GPU per slice at a time (its
    retouch job, then its standardise job, each within its timeout), never past the window and at most RUN_GPU_VCPUS
    worth at once; plus the allowance of every job that can start, which is each of its 2 jobs per slice. ran_usd
    charges exactly these parts, so a round never settles above what it reserved."""
    at_once = max(1, C.RUN_GPU_VCPUS // C.GPU_JOB_VCPUS)
    running = min(n_slices * min(C.RETOUCH_TIMEOUT + C.STANDARDISE_TIMEOUT, window), at_once * window)
    return running + 2 * n_slices * C.GPU_JOB_ALLOWANCE_SECONDS


def fitting(n_slices, window, left):
    """The most of the round's slices whose worst case the budget left covers (the cost only grows with the slices)."""
    low, high = 0, n_slices
    while low < high:
        mid = (low + high + 1) // 2
        if gpu_usd(worst_seconds(mid, window)) <= left:
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


def run_round(batch, s3, tag, todo, watermarked, run_id, deadline, spend, clock=time.monotonic, now=time.time,
              sleep=time.sleep):
    """Every listed photo through retouch and standardise under `tag`: [(job name, why)] of failed jobs. No jobs, no
    ceiling change, when nothing is listed. Skipped when less than GPU_MIN_WINDOW_SECONDS of the work window (on `clock`)
    is left. The round is sized to the month's budget left: as many whole slices as their worst case fits, the rest
    waits (SpendCapReached when not one fits: nothing is submitted); that worst case is reserved first and settled to
    what the jobs ran, however the round ends. It waits until the deadline at most: then its jobs are stopped
    (TimeoutError), every photo they finished stays, the next run goes on."""
    if not any(todo.values()):
        return []
    window = max(0.0, deadline - clock())
    if window < C.GPU_MIN_WINDOW_SECONDS:
        raise Skipped(f"only {window:.0f}s of the work window left (a round needs {C.GPU_MIN_WINDOW_SECONDS}s)")
    queue, jobdef = target()
    plan = slices(todo)
    todo = _first(plan[:max(1, fitting(len(plan), window, spend.left()))])
    reserved = gpu_usd(worst_seconds(len(slices(todo)), window))
    spend.reserve(reserved)                            # not even one slice fits: SpendCapReached, nothing submitted
    ids = []
    try:
        with raised(batch, pool(batch, queue)):
            try:
                submit(batch, s3, queue, jobdef, tag, todo, watermarked, run_id, ids)
                return wait(batch, ids, min(wait_limit(len(slices(todo))), window))
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


def clear_leftovers(batch, run_id):
    """Stop the jobs an earlier run left still active (its process was killed before it could) and set the GPU pool's
    ceiling back to idle: [the stopped job ids]. Only one process runs at a time (the run lock), so every active job of
    the process that is not this run's was left."""
    queue, _ = target()
    left = [j["jobId"] for j in _listed(batch, queue)
            if j["status"] not in _DONE and not j["jobName"].startswith(f"{C.JOB_PREFIX}{run_id}-")]
    stop(batch, left, f"left by an earlier standardise run; stopped by {run_id}")
    _ceiling(batch, pool(batch, queue), C.IDLE_GPU_VCPUS)
    return left
