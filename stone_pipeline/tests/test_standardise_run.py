"""The steps of the standardise process, with AWS stand-ins: the GPU round is sliced, each retouch job is followed by its
standardise job, the pool's ceiling is always set back, the steps run in order inside the work window and the budget,
a photo left without a result is reported, only the listings' first photos the standardiser has not run go to the GPU,
and one process runs at a time (a killed one's lock is taken over)."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

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
        return {"jobs": [{"jobId": j, "jobName": j, "status": statuses[min(i, len(statuses) - 1)], "statusReason": "boom",
                          **self.times.get(j, {})} for i, j in enumerate(jobs)]}

    times = {}


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
    ids = gpu.submit(batch, s3, "q", "jd", "v9", {"sentas": [A, B, D], "varsha": [E]}, {"sentas": False, "varsha": True}, "R1", [])
    names = [j["jobName"].removeprefix(f"{C.JOB_PREFIX}R1-") for j in batch.submitted]   # named by their run
    assert names == ["retouch-sentas-v9-0", "standardise-sentas-v9-0", "retouch-sentas-v9-1", "standardise-sentas-v9-1",
                     "retouch-varsha-v9-0", "standardise-varsha-v9-0"]
    assert all(j["jobName"].startswith(f"{C.JOB_PREFIX}R1-") for j in batch.submitted)
    assert batch.submitted[1]["dependsOn"] == [{"jobId": ids[0]}]                     # standardise waits for its retouch
    envs = [{e["name"]: e["value"] for e in j["containerOverrides"]["environment"]} for j in batch.submitted]
    assert envs[0]["ONLY_KEY"] == envs[1]["ONLY_KEY"] != envs[2]["ONLY_KEY"]            # one list per slice, both jobs
    assert [json.loads(s3.objects[e["ONLY_KEY"]]) for e in envs[::2]] == [[A, B], [D], [E]]
    assert "SLICE_OFFSET" not in envs[0] and envs[5]["WATERMARKED"] == "true" and envs[5]["RUN_MODE"] == "standardise"


def test_waiting_gives_up_after_the_deadline():
    times = iter([0, 50, 150])
    with pytest.raises(TimeoutError):
        gpu.wait(_Batch(polls=[("RUNNING",)]), ["j1"], 100, sleep=lambda s: None, clock=lambda: next(times))


def test_the_deadline_grows_with_each_wave_of_slices_that_cannot_run_at_once(monkeypatch):
    monkeypatch.setattr(C, "RUN_GPU_VCPUS", 128)
    monkeypatch.setattr(C, "GPU_JOB_VCPUS", 4)
    assert gpu.wait_limit(32) == C.RUN_MAX_WAIT_SECONDS and gpu.wait_limit(33) == 2 * C.RUN_MAX_WAIT_SECONDS


def test_a_round_that_fails_while_waiting_stops_its_jobs_and_sets_the_ceiling_back(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    batch, stopped = _Batch(polls=[("RUNNING",)]), []
    batch.terminate_job = lambda jobId, reason: stopped.append(jobId)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, limit: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, {"sentas": False}, "R1", math.inf, sel_mod.Spend(100))
    assert stopped == ["job1", "job2"] and batch.ceilings == [C.RUN_GPU_VCPUS, C.IDLE_GPU_VCPUS]


def test_a_submit_that_fails_part_way_still_stops_the_jobs_it_submitted(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    monkeypatch.setattr(C, "RUN_SLICE", 1)
    batch, stopped = _Batch(polls=[("RUNNING",)]), []
    batch.terminate_job = lambda jobId, reason: stopped.append(jobId)
    submit_job = batch.submit_job
    batch.submit_job = lambda **job: submit_job(**job) if len(batch.submitted) < 3 else (_ for _ in ()).throw(RuntimeError("throttled"))
    with pytest.raises(RuntimeError):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A, B]}, {"sentas": False}, "R1", math.inf, sel_mod.Spend(100))
    assert stopped == ["job1", "job2", "job3"]


def test_stopping_stops_every_job_and_goes_on_past_one_that_cannot_be_stopped():
    from botocore.exceptions import ClientError
    batch, stopped = _Batch(), []

    def terminate(jobId, reason):
        if jobId == "j2":
            raise ClientError({"Error": {"Code": "ClientException"}}, "TerminateJob")
        stopped.append(jobId)
    batch.terminate_job = terminate
    gpu.stop(batch, ["j1", "j2", "j3"], "stopped")
    assert stopped == ["j1", "j3"]


def test_a_photo_s_own_failure_is_held_but_a_broken_gpu_or_passing_storage_error_stops_the_slice_for_a_retry():
    from botocore.exceptions import ClientError
    from deploy import standardise_source as source
    slow = ClientError({"Error": {"Code": "SlowDown"}}, "GetObject")
    missing = ClientError({"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")
    assert source.after_failure(ValueError("undecodable image"), 0, False) == "hold"
    assert source.after_failure(missing, 0, False) == "hold"                         # its own input is missing: held
    assert source.after_failure(RuntimeError("CUDA error: an illegal memory access"), 0, False) == "stop"
    assert source.after_failure(RuntimeError("cuDNN error: CUDNN_STATUS_INTERNAL_ERROR"), 0, False) == "stop"
    assert source.after_failure(RuntimeError("CUDA error: device-side assert"), 0, True) == "hold"   # crashed it twice
    assert source.after_failure(slow, 1, False) == "retry"
    assert source.after_failure(slow, C.SLICE_MAX_STORAGE_FAILURES, False) == "stop"


def test_waiting_asks_until_every_job_has_ended_and_names_the_failed(monkeypatch):
    batch = _Batch(polls=[("RUNNING",), ("SUCCEEDED", "FAILED")])
    slept = []
    assert gpu.wait(batch, ["j1", "j2"], 3600, sleep=slept.append) == [("j2", "boom")]
    assert slept == [C.RUN_POLL_SECONDS]


def test_the_pool_s_ceiling_is_set_back_even_when_the_round_fails():
    batch = _Batch()
    with pytest.raises(RuntimeError):
        with gpu.raised(batch, "gpu-pool"):
            raise RuntimeError("interrupted")
    assert batch.ceilings == [C.RUN_GPU_VCPUS, C.IDLE_GPU_VCPUS]


def test_a_round_with_nothing_listed_touches_nothing():
    batch = _Batch()
    assert gpu.run_round(batch, _S3(), "v9", {"sentas": []}, {"sentas": False}, "R1", math.inf, sel_mod.Spend(0)) == []
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
    assert s.gpu_todo() == {"sentas": [photos[2], photos[3]]}       # 1 done, 2 excluded (so 4 is a candidate), 5+ beyond


def test_only_cleaned_copies_not_yet_run_go_to_the_gpu():
    s = _selection([_listing(A, B)], clean_round=_round(finished={A}, cleaned={A, B}))
    assert s.clean_todo() == {"sentas": [B]}


# --- the run --------------------------------------------------------------------------------------------------------
def _steps(monkeypatch, cap_at=None):
    done = []

    def step(name):
        def run(sel, spend, deadline):
            done.append(name)
            return [], name == cap_at
        return run
    monkeypatch.setattr(runner.select, "judge_all", step("judge"))
    monkeypatch.setattr(runner.select, "clean_all", step("clean"))
    monkeypatch.setattr(runner.select, "report", lambda sel: done.append("report") or {"chosen": [], "tracked": [], "pending": []})
    monkeypatch.setattr(runner, "gpu_round", lambda batch, sel, tag, todo, run_id, deadline, spend: (done.append(f"gpu {tag}"), ([], False))[1])
    return done


def test_the_steps_run_in_order(monkeypatch):
    done = _steps(monkeypatch)
    problems, _ = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), "R1", math.inf, say=lambda m: None)
    assert done == ["gpu v9", "judge", "clean", "gpu v9-clean", "judge", "report"] and not problems


def test_with_the_clean_up_off_no_photo_is_sent_to_a_hosted_edit(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_ENABLED", False)
    done = _steps(monkeypatch)
    runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), "R1", math.inf, say=lambda m: None)
    assert done == ["gpu v9", "judge", "gpu v9-clean", "judge", "report"]


def test_the_judge_bar_accepts_the_corrected_proportions_and_what_a_buyer_would_not_see():
    assert C.JUDGE_VERSION == 3
    prompt = C.JUDGE_PROMPT.lower()
    assert "real dimensions" in prompt and "never wrong_shape" in prompt
    assert "every edge and corner the slab has in image 1 must still be in image 2" in prompt   # a crop is not scaling
    assert "for a slab, the whole slab at a different width-to-height ratio" in prompt          # blocks and tiles excluded
    assert "at catalog size" in prompt


def test_the_run_stops_its_steps_at_the_budget_and_still_reports_what_is_chosen(monkeypatch):
    done = _steps(monkeypatch, cap_at="clean")
    runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), "R1", math.inf, say=lambda m: None)
    assert done == ["gpu v9", "judge", "clean", "report"]


def test_listings_left_not_fully_judged_are_one_problem(monkeypatch):
    _steps(monkeypatch)
    monkeypatch.setattr(runner.select, "report", lambda sel: {"chosen": [], "tracked": [], "pending": ["slab-1", "slab-2"]})
    problems, _ = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), "R1", math.inf, say=lambda m: None)
    assert problems == ["2 listings not fully judged (listed in r/summary.json)"]


def test_no_step_starts_after_the_work_window_and_the_report_still_runs(monkeypatch):
    done = _steps(monkeypatch)
    times = iter([0, 0, 100])                                       # the window (50 s) ends during the judge step
    problems, _ = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), "R1", 50, say=lambda m: None,
                             clock=lambda: next(times))
    assert done == ["gpu v9", "judge", "report"]
    assert problems == [f"the work window ({C.WORK_SECONDS}s) ended: the next run goes on"]


def test_a_listed_photo_left_without_a_result_is_a_problem(monkeypatch):
    monkeypatch.setattr(runner.gpu, "run_round", lambda *a: [])
    monkeypatch.setattr(runner, "load_source", lambda src: type("S", (), {"watermarked": False})())
    s = _selection([_listing(A, B)], gpu_round=_round(finished={A}))
    problems, stopped = runner.gpu_round(_Batch(), s, "v9", {"sentas": [A, B]}, "R1", math.inf, sel_mod.Spend(10))
    assert problems == [f"sentas {B[:12]}: no result after the GPU round"] and not stopped


def test_a_gpu_round_stopped_at_the_window_is_a_problem_and_the_run_goes_on(monkeypatch):
    def timed_out(*a):
        raise TimeoutError("GPU jobs not done after 600s")
    monkeypatch.setattr(runner.gpu, "run_round", timed_out)
    monkeypatch.setattr(runner, "load_source", lambda src: type("S", (), {"watermarked": False})())
    s = _selection([_listing(A)], gpu_round=_round(finished={A}))
    assert runner.gpu_round(_Batch(), s, "v9", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(10)) == (
        ["GPU round v9 stopped: GPU jobs not done after 600s"], False)


def test_a_gpu_round_the_budget_cannot_cover_stops_the_run_s_steps(monkeypatch):
    def capped(*a):
        raise sel_mod.SpendCapReached("hosted spend would pass the month's budget")
    monkeypatch.setattr(runner.gpu, "run_round", capped)
    monkeypatch.setattr(runner, "load_source", lambda src: type("S", (), {"watermarked": False})())
    s = _selection([_listing(A)])
    problems, stopped = runner.gpu_round(_Batch(), s, "v9", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(10))
    assert stopped and problems == ["GPU round v9 not started: hosted spend would pass the month's budget"]


def test_a_gpu_round_waits_no_longer_than_the_work_window(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    limits = []
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, limit: limits.append(limit) or [])
    gpu.run_round(_Batch(), _S3(), "v9", {"sentas": [A]}, {"sentas": False}, "R1", 1000, sel_mod.Spend(100), clock=lambda: 400)
    assert limits == [600]


# --- the GPU's share of the month's budget ----------------------------------------------------------------------------
def _gpu_env(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    monkeypatch.setattr(C, "GPU_VCPU_HOUR_USD", 0.25)
    monkeypatch.setattr(C, "GPU_JOB_VCPUS", 4)                                      # 1.00 USD per GPU-hour


def test_a_round_reserves_its_worst_case_one_gpu_per_slice_within_the_window(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(C, "RUN_GPU_VCPUS", 8)                                      # 2 GPUs at once
    longest = C.RETOUCH_TIMEOUT + C.STANDARDISE_TIMEOUT
    assert gpu.worst_seconds(1, 10 * longest) == longest                           # one slice: its two jobs, in turn
    assert gpu.worst_seconds(1, 600) == 600                                        # never past the window
    assert gpu.worst_seconds(5, 600) == 2 * 600                                    # at most 2 GPUs at once
    assert gpu.gpu_usd(3600) == pytest.approx(1.0)


def test_a_round_settles_what_its_jobs_ran_from_their_start_and_stop(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, limit: [])
    batch = _Batch()
    batch.times = {"job1": {"startedAt": 0, "stoppedAt": 1_800_000}, "job2": {"startedAt": 1_800_000, "stoppedAt": 5_400_000}}
    kept = []
    spend = sel_mod.Spend(100, save=kept.append)
    gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, {"sentas": False}, "R1", 7200, spend, clock=lambda: 0)
    assert kept[0] == pytest.approx(2.0)                                           # reserved: one slice, the 2 h window
    assert spend.spent == pytest.approx(1.5) and spend.reserved == 0               # ran 0.5 h + 1 h


def test_a_round_the_budget_cannot_cover_submits_nothing(monkeypatch):
    _gpu_env(monkeypatch)
    batch = _Batch()
    with pytest.raises(sel_mod.SpendCapReached):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, {"sentas": False}, "R1", 7200, sel_mod.Spend(1.0), clock=lambda: 0)
    assert batch.submitted == [] and batch.ceilings == []


def test_a_stopped_round_still_settles_counting_a_job_still_stopping_to_now(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, limit: (_ for _ in ()).throw(SystemExit("stopped by signal 15")))
    batch = _Batch()
    batch.terminate_job = lambda jobId, reason: None
    batch.times = {"job1": {"startedAt": 0}}                                       # running when it was told to stop
    spend = sel_mod.Spend(100)
    with pytest.raises(SystemExit):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, {"sentas": False}, "R1", 7200, spend, clock=lambda: 0,
                      now=lambda: 900)
    assert spend.spent == pytest.approx(0.25) and spend.reserved == 0


def test_when_batch_cannot_say_what_ran_the_round_counts_what_it_reserved(monkeypatch):
    from botocore.exceptions import ClientError
    _gpu_env(monkeypatch)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, limit: [])
    batch = _Batch()
    batch.describe_jobs = lambda jobs: (_ for _ in ()).throw(ClientError({"Error": {"Code": "ServerException"}}, "DescribeJobs"))
    spend = sel_mod.Spend(100)
    gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, {"sentas": False}, "R1", 7200, spend, clock=lambda: 0)
    assert spend.spent == pytest.approx(2.0)


# --- what an earlier run left -----------------------------------------------------------------------------------------
def test_jobs_an_earlier_run_left_are_stopped_and_the_ceiling_set_back(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    batch, stopped, asked = _Batch(), [], []
    batch.terminate_job = lambda jobId, reason: stopped.append(jobId)
    pages = [{"jobSummaryList": [{"jobId": "old1", "jobName": f"{C.JOB_PREFIX}R0-retouch-sentas-v2-0", "status": "RUNNING"}],
              "nextToken": "t"},
             {"jobSummaryList": [{"jobId": "old2", "jobName": f"{C.JOB_PREFIX}R0-standardise-sentas-v2-0", "status": "SUCCEEDED"},
                                 {"jobId": "old3", "jobName": f"{C.JOB_PREFIX}R0-standardise-sentas-v2-1", "status": "RUNNABLE"}]}]
    batch.list_jobs = lambda **kw: asked.append(kw) or pages[len(asked) - 1]
    assert gpu.clear_leftovers(batch, "R1") == ["old1", "old3"]
    assert stopped == ["old1", "old3"] and batch.ceilings == [C.IDLE_GPU_VCPUS]
    assert asked[0]["filters"] == [{"name": "JOB_NAME", "values": [f"{C.JOB_PREFIX}*"]}] and asked[1]["nextToken"] == "t"


def test_a_failed_hosted_step_is_run_once_more():
    calls = []

    def flaky(sel, spend, deadline):
        calls.append(1)
        return ([("slab-1", "timeout")] if len(calls) == 1 else []), False
    assert runner.hosted(flaky, None, None, math.inf) == ([], False) and len(calls) == 2


# --- one run at a time ------------------------------------------------------------------------------------------------
NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)


class _LockS3:
    """A bucket with conditional writes and deletes as S3 does them: IfNoneMatch fails when the key exists, IfMatch when
    its ETag is another. `age` = how long ago the held lock was last written."""

    def __init__(self, held=False, age=0):
        self.locks = {runner.LOCK: {"owner": "R0", "etag": '"e0"', "at": NOW - timedelta(seconds=age)}} if held else {}
        self.writes = 0

    def _check(self, Key, IfNoneMatch=None, IfMatch=None):
        from botocore.exceptions import ClientError
        held = self.locks.get(Key)
        if (IfNoneMatch and held) or (IfMatch and (not held or held["etag"] != IfMatch)):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None):
        self._check(Key, IfNoneMatch, IfMatch)
        self.writes += 1
        self.locks[Key] = {"owner": json.loads(Body)["run_id"], "etag": f'"w{self.writes}"', "at": NOW}
        return {"ETag": self.locks[Key]["etag"]}

    def head_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.locks:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ETag": self.locks[Key]["etag"], "LastModified": self.locks[Key]["at"]}

    def delete_object(self, Bucket, Key, IfMatch=None):
        self._check(Key, IfMatch=IfMatch)
        self.locks.pop(Key, None)

    @property
    def keys(self):
        return set(self.locks)


STALE = 4 * 3600                                                    # the runner's ceiling (SCRAPER_RUN_TIMEOUT_SECONDS)


@pytest.fixture(autouse=True)
def _runner_ceiling(monkeypatch):
    monkeypatch.setattr(runner, "RUN_TIMEOUT_SECONDS", STALE)


def test_a_second_run_refuses_to_start_while_one_holds_the_lock():
    with pytest.raises(runner.RunActive):
        with runner.exclusive(_LockS3(held=True, age=STALE - 60), {"run_id": "R2"}, now=NOW):
            pytest.fail("ran alongside another run")


def test_a_lock_not_refreshed_for_the_runner_s_ceiling_belongs_to_a_killed_run_and_is_taken_over():
    s3 = _LockS3(held=True, age=STALE + 60)
    with runner.exclusive(s3, {"run_id": "R2"}, now=NOW):
        assert s3.locks[runner.LOCK]["owner"] == "R2"
    assert runner.LOCK not in s3.keys


def test_of_two_runs_taking_over_a_stale_lock_only_one_does():
    s3 = _LockS3(held=True, age=STALE + 60)
    head = s3.head_object
    s3.head_object = lambda Bucket, Key: dict(head(Bucket, Key), ETag='"taken-by-another"')   # it moved after we looked
    with pytest.raises(runner.RunActive):
        with runner.exclusive(s3, {"run_id": "R3"}, now=NOW):
            pytest.fail("two runs took over the same lock")


def test_a_lock_released_while_we_looked_at_it_is_taken_on_the_next_try():
    s3 = _LockS3(held=True, age=60)
    head = s3.head_object

    def released(Bucket, Key):
        s3.locks.pop(Key)                                           # the other run ended between our write and our look
        return head(Bucket, Key)
    s3.head_object = released
    with runner.exclusive(s3, {"run_id": "R2"}, now=NOW):
        assert s3.locks[runner.LOCK]["owner"] == "R2"


def test_the_lock_is_released_even_when_the_run_fails():
    s3 = _LockS3()
    with pytest.raises(RuntimeError):
        with runner.exclusive(s3, {"run_id": "R1"}):
            assert runner.LOCK in s3.keys
            raise RuntimeError("interrupted")
    assert runner.LOCK not in s3.keys


def test_a_lock_another_run_took_over_is_never_removed():
    s3 = _LockS3()
    with runner.exclusive(s3, {"run_id": "R1"}):
        s3.locks[runner.LOCK] = {"owner": "R9", "etag": '"theirs"', "at": NOW}
    assert s3.locks[runner.LOCK]["owner"] == "R9"


def test_the_lock_is_refreshed_while_the_run_works_and_a_lost_lock_stops_its_paid_calls():
    import time
    s3, lost = _LockS3(), []
    with runner.exclusive(s3, {"run_id": "R1"}, on_lost=lambda: lost.append(1), beat=0.02):
        time.sleep(0.1)
        assert s3.writes >= 3                                       # written, then refreshed
        s3.locks[runner.LOCK] = {"owner": "R9", "etag": '"theirs"', "at": NOW}
        time.sleep(0.1)
    assert lost == [1] and s3.locks[runner.LOCK]["owner"] == "R9"


def test_a_run_s_size_is_set_per_run_and_a_malformed_value_fails_loud(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setenv("SCRAPER_STANDARDISE_GPU_VCPUS", "384")
    assert settings._env_int("SCRAPER_STANDARDISE_GPU_VCPUS", 128) == 384
    assert settings._env_int("SCRAPER_STANDARDISE_RUN_SLICE", 150) == 150          # unset: the default
    monkeypatch.setenv("SCRAPER_STANDARDISE_RUN_SLICE", "forty")
    with pytest.raises(ValueError, match="SCRAPER_STANDARDISE_RUN_SLICE"):
        settings._env_int("SCRAPER_STANDARDISE_RUN_SLICE", 150)
    assert C.IDLE_GPU_VCPUS == 8                                                    # always back to idle after a run
