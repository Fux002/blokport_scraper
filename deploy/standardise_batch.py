"""One GPU round of a standardise run on AWS Batch: the listed photos of each source, in slices of RUN_SLICE, each slice a
retouch job then a standardise job that starts when the retouch ended, both reading the slice's own fixed photo list
(ONLY_KEY). The GPU pool's ceiling is raised for the round and always set back; a round that fails, times out or is
stopped stops its jobs. Queue and job definition come from the
environment (SCRAPER_GPU_QUEUE, SCRAPER_STANDARDISE_JOBDEF), never named here."""

from __future__ import annotations

import json
import time
from contextlib import contextmanager

from botocore.exceptions import ClientError

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
        retouch = batch.submit_job(jobName=f"retouch-{name}", jobQueue=queue, jobDefinition=jobdef,
                                   timeout={"attemptDurationSeconds": C.RETOUCH_TIMEOUT},
                                   containerOverrides={"environment": [{"name": "RUN_MODE", "value": "standardise-retouch"}] + common})["jobId"]
        ids.append(retouch)
        standardise = batch.submit_job(jobName=f"standardise-{name}", jobQueue=queue, jobDefinition=jobdef,
                                       dependsOn=[{"jobId": retouch}], timeout={"attemptDurationSeconds": C.STANDARDISE_TIMEOUT},
                                       containerOverrides={"environment": [{"name": "RUN_MODE", "value": "standardise"},
                                                                           {"name": "WATERMARKED", "value": str(watermarked[src]).lower()}]
                                                           + common})["jobId"]
        ids.append(standardise)
    return ids


def wait(batch, ids, sleep=time.sleep, clock=time.monotonic):
    """Block until every job has ended: [(job name, why)] of the jobs that failed. TimeoutError after
    RUN_MAX_WAIT_SECONDS (jobs stuck waiting for GPUs never end on their own)."""
    deadline = clock() + C.RUN_MAX_WAIT_SECONDS
    while True:
        jobs = [j for i in range(0, len(ids), 100) for j in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]]
        if all(j["status"] in _DONE for j in jobs):
            return [(j["jobName"], j.get("statusReason", "")) for j in jobs if j["status"] == "FAILED"]
        if clock() > deadline:
            raise TimeoutError(f"GPU jobs not done after {C.RUN_MAX_WAIT_SECONDS}s")
        sleep(C.RUN_POLL_SECONDS)


def stop(batch, ids, why):
    """Stop every job of the round that has not ended (a run that fails or is stopped leaves no GPU work behind). A job
    that cannot be stopped is reported and the others are still stopped."""
    for i in range(0, len(ids), 100):
        for job in batch.describe_jobs(jobs=ids[i:i + 100])["jobs"]:
            if job["status"] in _DONE:
                continue
            try:
                batch.terminate_job(jobId=job["jobId"], reason=why[:250])
            except ClientError as exc:
                print(f"   could not stop GPU job {job['jobName']}: {exc}", flush=True)


def run_round(batch, s3, tag, todo, watermarked, run_id):
    """Every listed photo through retouch and standardise under `tag`: [(job name, why)] of failed jobs. No jobs, no
    ceiling change, when nothing is listed."""
    if not any(todo.values()):
        return []
    queue, jobdef = target()
    with raised(batch, pool(batch, queue)):
        ids = []
        try:
            return wait(batch, submit(batch, s3, queue, jobdef, tag, todo, watermarked, run_id, ids))
        except BaseException as exc:
            stop(batch, ids, f"standardise run {run_id} stopped: {type(exc).__name__}")
            raise
