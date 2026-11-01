"""A standardise run end to end, with AWS stand-ins: the GPU round is sliced, each retouch job is followed by its
standardise job, the pool's ceiling is always set back, the steps run in order, a photo left without a result and the
spend cap are reported, and only the listings' first photos the standardiser has not run go to the GPU."""
from __future__ import annotations

import json

import pytest

from deploy import standardise_batch as gpu
from deploy import standardise_run as runner
from deploy import standardise_select as sel_mod
from stone_pipeline.config.settings import STANDARDISE as C

A, B, D, E = "a" * 64, "b" * 64, "d" * 64, "e" * 64


class _Batch:
    """Records what a run asks of Batch; jobs end with the statuses given, in turn per poll."""

    def __init__(self, polls=(("SUCCEEDED",),)):
        self.submitted, self.ceilings, self.polls = [], [], list(polls)

    def describe_job_queues(self, jobQueues):
        return {"jobQueues": [{"computeEnvironmentOrder": [{"computeEnvironment": "gpu-pool"}]}]}

    def update_compute_environment(self, computeEnvironment, computeResources):
        self.ceilings.append(computeResources["maxvCpus"])

    def submit_job(self, **job):
        self.submitted.append(job)
        return {"jobId": f"job{len(self.submitted)}"}

    def describe_jobs(self, jobs):
        statuses = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
        return {"jobs": [{"jobName": j, "status": statuses[min(i, len(statuses) - 1)], "statusReason": "boom"}
                         for i, j in enumerate(jobs)]}


class _S3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body


def _round(finished=(), held=(), cleaned=()):
    r = object.__new__(sel_mod.Round)
    r.prefix, r.finished, r.held, r.cleaned = "p/", set(finished), set(held), set(cleaned)
    r.judged, r.attempts, r.edited = set(), set(), set()
    return r


def _selection(listings, gpu_round=None, clean_round=None, exclude=()):
    s = object.__new__(sel_mod.Selection)
    s.client, s.listings, s.tags, s.exclude, s.root = _S3(), listings, ("v9", "v9-clean"), set(exclude), "r/"
    s.sources = sorted({l["source"] for l in listings})
    s.rounds = [("v9", {"sentas": gpu_round or _round()}), ("v9-clean", {"sentas": clean_round or _round()})]
    s.refresh = lambda: None
    return s


def _listing(*photos, key="slab-1"):
    return {"key": key, "source": "sentas", "format": "slab", "photos": list(photos)}


# --- the GPU round --------------------------------------------------------------------------------------------------
def test_each_slice_has_its_own_fixed_list_and_is_a_retouch_then_a_standardise(monkeypatch):
    monkeypatch.setattr(C, "RUN_SLICE", 2)
    batch, s3 = _Batch(), _S3()
    ids = gpu.submit(batch, s3, "q", "jd", "v9", {"sentas": [A, B, D], "varsha": [E]}, {"sentas": False, "varsha": True}, "R1")
    names = [j["jobName"] for j in batch.submitted]
    assert names == ["retouch-sentas-v9-0", "standardise-sentas-v9-0", "retouch-sentas-v9-1", "standardise-sentas-v9-1",
                     "retouch-varsha-v9-0", "standardise-varsha-v9-0"]
    assert batch.submitted[1]["dependsOn"] == [{"jobId": ids[0]}]                     # standardise waits for its retouch
    envs = [{e["name"]: e["value"] for e in j["containerOverrides"]["environment"]} for j in batch.submitted]
    assert envs[0]["ONLY_KEY"] == envs[1]["ONLY_KEY"] != envs[2]["ONLY_KEY"]            # one list per slice, both jobs
    assert [json.loads(s3.objects[e["ONLY_KEY"]]) for e in envs[::2]] == [[A, B], [D], [E]]
    assert "SLICE_OFFSET" not in envs[0] and envs[5]["WATERMARKED"] == "true" and envs[5]["RUN_MODE"] == "standardise"


def test_waiting_gives_up_after_the_deadline(monkeypatch):
    monkeypatch.setattr(C, "RUN_MAX_WAIT_SECONDS", 100)
    times = iter([0, 50, 150])
    with pytest.raises(TimeoutError):
        gpu.wait(_Batch(polls=[("RUNNING",)]), ["j1"], sleep=lambda s: None, clock=lambda: next(times))


def test_a_round_that_fails_while_waiting_stops_its_jobs_and_sets_the_ceiling_back(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    batch, stopped = _Batch(), []
    batch.terminate_job = lambda jobId, reason: stopped.append(jobId)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, {"sentas": False}, "R1")
    assert stopped == ["job1", "job2"] and batch.ceilings == [C.RUN_GPU_VCPUS, C.IDLE_GPU_VCPUS]


def test_a_run_of_a_source_with_no_listings_refuses_to_start(monkeypatch):
    monkeypatch.setattr(runner, "sheet_listings", lambda s3: [_listing(A)])
    with pytest.raises(ValueError, match="no listings for sentsa"):
        runner.listings(None, {"sentas", "sentsa"}, "")


def test_waiting_asks_until_every_job_has_ended_and_names_the_failed(monkeypatch):
    batch = _Batch(polls=[("RUNNING",), ("SUCCEEDED", "FAILED")])
    slept = []
    assert gpu.wait(batch, ["j1", "j2"], sleep=slept.append) == [("j2", "boom")]
    assert slept == [C.RUN_POLL_SECONDS]


def test_the_pool_s_ceiling_is_set_back_even_when_the_round_fails():
    batch = _Batch()
    with pytest.raises(RuntimeError):
        with gpu.raised(batch, "gpu-pool"):
            raise RuntimeError("interrupted")
    assert batch.ceilings == [C.RUN_GPU_VCPUS, C.IDLE_GPU_VCPUS]


def test_a_round_with_nothing_listed_touches_nothing():
    batch = _Batch()
    assert gpu.run_round(batch, _S3(), "v9", {"sentas": []}, {"sentas": False}, "R1") == []
    assert batch.submitted == [] and batch.ceilings == []


def test_the_queue_and_job_definition_must_be_configured(monkeypatch):
    for name in ("SCRAPER_GPU_QUEUE", "BLOKPORT_GPU_QUEUE", "SCRAPER_STANDARDISE_JOBDEF", "BLOKPORT_STANDARDISE_JOBDEF"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(KeyError):
        gpu.target()


# --- what goes to the GPU ---------------------------------------------------------------------------------------------
def test_only_each_listing_s_first_photos_not_yet_run_go_to_the_gpu():
    photos = [c * 64 for c in "123456"]
    s = _selection([_listing(*photos)], gpu_round=_round(finished={photos[0]}), exclude={photos[1]})
    assert s.gpu_todo() == {"sentas": [photos[2]]}                  # 1 done, 2 excluded, 4+ beyond the judge's reach


def test_only_cleaned_copies_not_yet_run_go_to_the_gpu():
    s = _selection([_listing(A, B)], clean_round=_round(finished={A}, cleaned={A, B}))
    assert s.clean_todo() == {"sentas": [B]}


# --- the run --------------------------------------------------------------------------------------------------------
def _steps(monkeypatch, cap_at=None):
    done = []

    def step(name):
        def run(sel, spend):
            done.append(name)
            return [], name == cap_at
        return run
    monkeypatch.setattr(runner.select, "judge_all", step("judge"))
    monkeypatch.setattr(runner.select, "clean_all", step("clean"))
    monkeypatch.setattr(runner.select, "report", lambda sel: done.append("report") or {"chosen": [], "tracked": [], "pending": []})
    monkeypatch.setattr(runner, "gpu_round", lambda batch, sel, tag, todo, run_id: done.append(f"gpu {tag}") or [])
    return done


def test_the_steps_run_in_order(monkeypatch):
    done = _steps(monkeypatch)
    problems, capped = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), "R1", say=lambda m: None)
    assert done == ["gpu v9", "judge", "clean", "gpu v9-clean", "judge", "report"] and not problems and not capped


def test_the_run_stops_at_the_spend_cap_and_says_so(monkeypatch):
    done = _steps(monkeypatch, cap_at="clean")
    problems, capped = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), "R1", say=lambda m: None)
    assert capped and done == ["gpu v9", "judge", "clean"]


def test_a_listed_photo_left_without_a_result_is_a_problem(monkeypatch):
    monkeypatch.setattr(runner.gpu, "run_round", lambda *a: [])
    monkeypatch.setattr(runner, "load_source", lambda src: type("S", (), {"watermarked": False})())
    s = _selection([_listing(A, B)], gpu_round=_round(finished={A}))
    problems = runner.gpu_round(_Batch(), s, "v9", {"sentas": [A, B]}, "R1")
    assert problems == [f"sentas {B[:12]}: no result after the GPU round"]


def test_a_failed_hosted_step_is_run_once_more():
    calls = []

    def flaky(sel, spend):
        calls.append(1)
        return ([("slab-1", "timeout")] if len(calls) == 1 else []), False
    assert runner.hosted(flaky, None, None) == ([], False) and len(calls) == 2


# --- one run at a time ------------------------------------------------------------------------------------------------
class _LockS3:
    """A bucket where a conditional write fails when the key exists, as S3 does."""

    def __init__(self, held=False):
        self.keys = {runner.LOCK} if held else set()

    def put_object(self, Bucket, Key, Body, IfNoneMatch):
        from botocore.exceptions import ClientError
        if Key in self.keys:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.keys.add(Key)

    def delete_object(self, Bucket, Key):
        self.keys.discard(Key)


def test_a_second_run_refuses_to_start_while_one_holds_the_lock():
    with pytest.raises(runner.RunActive):
        with runner.exclusive(_LockS3(held=True), {"run_id": "R2"}):
            pytest.fail("ran alongside another run")


def test_the_lock_is_released_even_when_the_run_fails():
    s3 = _LockS3()
    with pytest.raises(RuntimeError):
        with runner.exclusive(s3, {"run_id": "R1"}):
            assert runner.LOCK in s3.keys
            raise RuntimeError("interrupted")
    assert runner.LOCK not in s3.keys
