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


@pytest.fixture(autouse=True)
def _judged_by_inputs(monkeypatch):
    """A judge or check request is its inputs here (judge.judging and judge.checking prepare the real one before anything
    is paid), so a fake judge receives (original, result, fmt) and a fake check (original, copy)."""
    monkeypatch.setattr(sel_mod.judge, "judging", lambda original, result, fmt: (original, result, fmt))
    monkeypatch.setattr(sel_mod.judge, "checking", lambda original, copy: (original, copy))


@pytest.fixture(autouse=True)
def _idle_ceiling(monkeypatch):
    """The idle ceiling the service's environment carries (Terraform's gpu_idle_vcpus); unset off AWS."""
    monkeypatch.setattr(C, "IDLE_GPU_VCPUS", 8)


@pytest.fixture(autouse=True)
def _prepared_by_name(monkeypatch):
    """An edit's prepared request is its model's name here (clean.prepare builds the real one before anything is paid),
    so a fake submit receives the model's name."""
    monkeypatch.setattr(sel_mod.clean, "prepare", lambda name, encoded: name)


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


def _round(finished=(), held=(), cleaned=(), slabs=(), studio=(), noslab=(), logo_step=(), checked=None):
    """checked: the whole slabs drawn under the current outline rule (default: every slab)."""
    r = object.__new__(sel_mod.Round)
    r.prefix, r.finished, r.held, r.cleaned = "p/", set(finished), set(held), set(cleaned)
    r.slabs, r.studio, r.noslab, r.logo_step = set(slabs), set(studio), set(noslab), set(logo_step)
    r.checked = set(slabs) if checked is None else set(checked)
    r.redrawn = set()
    r.judged, r.attempts, r.edited = set(), set(), set()
    return r


def _selection(listings, gpu_round=None, clean_round=None, exclude=(), chosen=()):
    s = object.__new__(sel_mod.Selection)
    s.client, s.listings, s.tags, s.exclude, s.root = _S3(), listings, ("v9", "v9-clean"), set(exclude), "r/"
    s.owner, s.conflicts, s.chosen, s.clicks = {}, {}, sorted(chosen), {}
    s.sources = sorted({l["source"] for l in listings} | {src for _, src, _ in s.chosen})
    s.rounds = [("v9", {"sentas": gpu_round or _round()}), ("v9-clean", {"sentas": clean_round or _round()})]
    s.refresh = lambda: None
    return s


def _listing(*photos, key="slab-1"):
    return {"key": key, "sku": "SEN-1", "source": "sentas", "format": "slab", "photos": list(photos)}


# --- the GPU round --------------------------------------------------------------------------------------------------
def test_each_slice_has_its_own_fixed_list_and_is_a_retouch_then_a_standardise(monkeypatch):
    monkeypatch.setattr(C, "RUN_SLICE", 2)
    batch, s3 = _Batch(), _S3()
    ids = gpu.submit(batch, s3, "q", "jd", "v9", {"sentas": [A, B, D], "varsha": [E]}, "R1", [])
    names = [j["jobName"].removeprefix(f"{C.JOB_PREFIX}R1-") for j in batch.submitted]   # named by their run
    assert names == ["retouch-sentas-v9-0", "standardise-sentas-v9-0", "retouch-sentas-v9-1", "standardise-sentas-v9-1",
                     "retouch-varsha-v9-0", "standardise-varsha-v9-0"]
    assert all(j["jobName"].startswith(f"{C.JOB_PREFIX}R1-") for j in batch.submitted)
    assert batch.submitted[1]["dependsOn"] == [{"jobId": ids[0]}]                     # standardise waits for its retouch
    envs = [{e["name"]: e["value"] for e in j["containerOverrides"]["environment"]} for j in batch.submitted]
    assert envs[0]["ONLY_KEY"] == envs[1]["ONLY_KEY"] != envs[2]["ONLY_KEY"]            # one list per slice, both jobs
    assert [json.loads(s3.objects[e["ONLY_KEY"]]) for e in envs[::2]] == [[A, B], [D], [E]]
    assert "SLICE_OFFSET" not in envs[0] and envs[5]["RUN_MODE"] == "standardise"


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
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(100),
                      sleep=lambda s: None)
    assert sorted(stopped) == ["job1", "job2"] and batch.ceilings == [C.RUN_GPU_VCPUS, C.IDLE_GPU_VCPUS]


def test_a_submit_that_fails_part_way_still_stops_the_jobs_it_submitted(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    monkeypatch.setattr(C, "RUN_SLICE", 1)
    batch, stopped = _Batch(polls=[("RUNNING",)]), []
    batch.terminate_job = lambda jobId, reason: stopped.append(jobId)
    submit_job = batch.submit_job
    batch.submit_job = lambda **job: submit_job(**job) if len(batch.submitted) < 3 else (_ for _ in ()).throw(RuntimeError("throttled"))
    with pytest.raises(RuntimeError):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A, B]}, "R1", math.inf, sel_mod.Spend(100),
                      sleep=lambda s: None)
    assert sorted(stopped) == ["job1", "job2", "job3"]                             # stopped in parallel, any order


def test_stopping_stops_every_job_and_goes_on_past_one_that_cannot_be_stopped():
    from botocore.exceptions import ClientError
    batch, stopped = _Batch(), []

    def terminate(jobId, reason):
        if jobId == "j2":
            raise ClientError({"Error": {"Code": "ClientException"}}, "TerminateJob")
        stopped.append(jobId)
    batch.terminate_job = terminate
    gpu.stop(batch, ["j1", "j2", "j3"], "stopped")
    assert sorted(stopped) == ["j1", "j3"]


def test_a_round_s_jobs_are_stopped_in_parallel():
    import threading
    import time
    batch, now, most, lock = _Batch(), [0], [0], threading.Lock()

    def terminate(jobId, reason):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        time.sleep(0.02)
        with lock:
            now[0] -= 1
    batch.terminate_job = terminate
    gpu.stop(batch, [f"j{i}" for i in range(40)], "stopped")
    assert most[0] > 1


def test_only_the_named_run_s_active_jobs_are_stopped(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    batch, stopped = _Batch(), []
    batch.terminate_job = lambda jobId, reason: stopped.append(jobId)
    batch.list_jobs = lambda **kw: {"jobSummaryList": [
        {"jobId": "mine", "jobName": f"{C.JOB_PREFIX}R1-retouch-sentas-v2-0", "status": "RUNNING"},
        {"jobId": "mine-done", "jobName": f"{C.JOB_PREFIX}R1-standardise-sentas-v2-0", "status": "SUCCEEDED"},
        {"jobId": "theirs", "jobName": f"{C.JOB_PREFIX}R2-retouch-sentas-v2-0", "status": "RUNNING"}]}
    assert gpu.stop_run(batch, "R1") == ["mine"] and stopped == ["mine"] and batch.ceilings == []


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
    assert gpu.wait(batch, ["j1", "j2"], 3600, sleep=slept.append, clock=lambda: 0) == [("j2", "boom")]
    assert slept == [C.RUN_POLL_SECONDS]


def test_the_pool_s_ceiling_is_set_back_even_when_the_round_fails():
    batch = _Batch()
    with pytest.raises(RuntimeError):
        with gpu.raised(batch, "gpu-pool"):
            raise RuntimeError("interrupted")
    assert batch.ceilings == [C.RUN_GPU_VCPUS, C.IDLE_GPU_VCPUS]


def test_a_round_with_nothing_listed_touches_nothing():
    batch = _Batch()
    assert gpu.run_round(batch, _S3(), "v9", {"sentas": []}, "R1", math.inf, sel_mod.Spend(0)) == []
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
        def run(sel, *given):                          # (spend, deadline), or (plans, spend, deadline) for clean_all
            done.append(name)
            return [], name == cap_at
        return run
    monkeypatch.setattr(runner.select, "settle", step("settle"))
    monkeypatch.setattr(runner.select, "judge_all", step("judge"))
    monkeypatch.setattr(runner.select, "clean_all", step("clean"))
    monkeypatch.setattr(runner.select, "report", lambda sel, waits, marks, merge: done.append("report") or {"chosen": [], "tracked": [], "pending": [], "click_problems": [], "review": {}})
    monkeypatch.setattr(runner, "gpu_round", lambda batch, sel, tag, todo, run_id, deadline, spend: (done.append(f"gpu {tag}"), ([], False))[1])
    return done


def test_the_steps_run_in_order(monkeypatch):
    done = _steps(monkeypatch)
    problems, _ = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), sel_mod.Spend(10), "R1", math.inf,
                             say=lambda m: None)
    assert done == ["gpu v9", "settle", "judge", "clean", "gpu v9-clean", "settle", "judge", "report"] and not problems


def test_without_a_cleaning_cap_no_photo_is_sent_to_a_hosted_edit(monkeypatch):
    # owner 2026-11-05: the clean-up edits have their own monthly cap; no cap set, no edit is paid
    done = _steps(monkeypatch)
    runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), None, "R1", math.inf, say=lambda m: None)
    assert done == ["gpu v9", "settle", "judge", "gpu v9-clean", "settle", "judge", "report"]


def test_the_clean_up_edits_are_paid_from_the_cleaning_cap_and_the_judge_from_the_process_budget(monkeypatch):
    process, cleaning = sel_mod.Spend(10), sel_mod.Spend(10)
    given = {}
    monkeypatch.setattr(runner, "gpu_round", lambda batch, sel, tag, todo, run_id, deadline, spend: ([], False))
    monkeypatch.setattr(runner.select, "settle", lambda sel, spend, deadline: ([], False))
    monkeypatch.setattr(runner.select, "judge_all", lambda sel, spend, deadline: given.setdefault("judge", spend) and ([], False))
    monkeypatch.setattr(runner.select, "clean_all", lambda sel, plans, spend, deadline: given.setdefault("clean", spend) and ([], False))
    monkeypatch.setattr(runner.select, "report", lambda sel, waits, marks, merge: {"chosen": [], "tracked": [], "pending": [], "click_problems": [], "review": {}})
    runner.run(_Batch(), _selection([_listing(A)]), process, cleaning, "R1", math.inf, say=lambda m: None)
    assert given["judge"] is process and given["clean"] is cleaning


def test_the_gpu_rounds_end_before_the_judge_s_share_of_the_window_and_the_judge_keeps_it(monkeypatch):
    # a GPU round that ran to the window's end left every photo it finished unjudged, so the run published none of them
    given = []
    monkeypatch.setattr(runner, "gpu_round", lambda batch, sel, tag, todo, run_id, deadline, spend: (given.append((tag, deadline)), ([], False))[1])
    monkeypatch.setattr(runner.select, "judge_all", lambda sel, spend, deadline: (given.append(("judge", deadline)), ([], False))[1])
    monkeypatch.setattr(runner.select, "clean_all", lambda sel, plans, spend, deadline: ([], False))
    monkeypatch.setattr(runner.select, "report", lambda sel, waits, marks, merge: {"chosen": [], "tracked": [], "pending": [], "click_problems": [], "review": {}})
    runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), None, "R1", 10_000, say=lambda m: None, clock=lambda: 0)
    gpu_end = 10_000 - C.JUDGE_WINDOW_SECONDS
    assert given == [("v9", gpu_end), ("judge", 10_000), ("v9-clean", gpu_end), ("judge", 10_000)]


def test_with_the_clean_switch_off_the_clean_step_still_runs_for_the_owner_s_clicks(monkeypatch):
    # owner 2026-11-05: the switch governs only the automatic edits (select.authorised); a click is cleaned either way
    monkeypatch.setattr(C, "CLEAN_ENABLED", False)
    done = _steps(monkeypatch)
    runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), sel_mod.Spend(10), "R1", math.inf, say=lambda m: None)
    assert done == ["gpu v9", "settle", "judge", "clean", "gpu v9-clean", "settle", "judge", "report"]


def test_the_judge_bar_accepts_the_corrected_proportions_and_what_a_buyer_would_not_see():
    assert C.JUDGE_VERSION == 3
    prompt = C.JUDGE_PROMPT.lower()
    assert "real dimensions" in prompt and "never wrong_shape" in prompt
    assert "every edge and corner the slab has in image 1 must still be in image 2" in prompt   # a crop is not scaling
    assert "for a slab, the whole slab at a different width-to-height ratio" in prompt          # blocks and tiles excluded
    assert "at catalog size" in prompt


def test_the_run_stops_its_steps_at_the_process_budget_and_still_reports_what_is_chosen(monkeypatch):
    done = _steps(monkeypatch, cap_at="judge")
    runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), sel_mod.Spend(10), "R1", math.inf, say=lambda m: None)
    assert done == ["gpu v9", "settle", "judge", "report"]


def test_a_reached_cleaning_cap_ends_only_the_clean_step(monkeypatch):
    # review of #524: the cap's error ended the whole run, so the cleaned copies made before it were never standardised
    # or judged, and every later run of the month did the same
    done = _steps(monkeypatch)
    monkeypatch.setattr(runner.select, "clean_all", lambda sel, plans, spend, deadline: (
        done.append("clean"), sel_mod._each([{"key": "k"}], lambda l: spend.reserve(1.0), deadline))[1])
    cap = sel_mod.Spend(0, cap_error=sel_mod.CleanCapReached)
    problems, _ = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(100), cap, "R1", math.inf,
                             say=lambda m: None)
    assert done == ["gpu v9", "settle", "judge", "clean", "gpu v9-clean", "settle", "judge", "report"]
    assert cap.capped and problems and all("cleaning cap" in p for p in problems)


def test_the_process_budget_still_ends_the_run_in_any_step(monkeypatch):
    done = _steps(monkeypatch)
    monkeypatch.setattr(runner.select, "judge_all", lambda sel, spend, deadline: (
        done.append("judge"), sel_mod._each([{"key": "k"}], lambda l: spend.reserve(1.0), deadline))[1])
    runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(0), sel_mod.Spend(10), "R1", math.inf, say=lambda m: None)
    assert done == ["gpu v9", "settle", "judge", "report"]


def test_listings_left_not_fully_judged_are_one_problem(monkeypatch):
    _steps(monkeypatch)
    monkeypatch.setattr(runner.select, "report", lambda sel, waits, marks, merge: {"chosen": [], "tracked": [], "pending": ["slab-1", "slab-2"], "click_problems": [], "review": {}})
    problems, _ = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), None, "R1", math.inf, say=lambda m: None)
    assert problems == ["2 listings not fully judged (listed in r/summary.json)"]


def test_no_step_starts_after_the_work_window_and_the_report_still_runs(monkeypatch):
    done = _steps(monkeypatch)
    times = iter([0, 0, 0, 100])                                    # the window (50 s) ends during the judge step
    problems, _ = runner.run(_Batch(), _selection([_listing(A)]), sel_mod.Spend(10), None, "R1", 50, say=lambda m: None,
                             clock=lambda: next(times))
    assert done == ["gpu v9", "settle", "judge", "report"]
    assert problems == [f"the work window ({C.WORK_SECONDS}s) ended: the next run goes on"]


def test_each_close_only_slice_has_its_own_fixed_list_and_is_named_by_the_run(monkeypatch):
    monkeypatch.setattr(C, "RUN_SLICE", 2)
    batch, s3 = _Batch(), _S3()
    gpu.submit_close(batch, s3, "q", "jd", "v9-clean", {"sentas": [A, B, D], "varsha": [E]}, "R1", [])
    envs = [{e["name"]: e["value"] for e in j["containerOverrides"]["environment"]} for j in batch.submitted]
    assert [j["jobName"] for j in batch.submitted] == [f"{C.JOB_PREFIX}R1-close-sentas-v9-clean-0", f"{C.JOB_PREFIX}R1-close-sentas-v9-clean-1",
                                                       f"{C.JOB_PREFIX}R1-close-varsha-v9-clean-0"]          # cleared by run id
    assert [json.loads(s3.objects[e["ONLY_KEY"]]) for e in envs] == [[A, B], [D], [E]]
    assert all(e["RUN_MODE"] == "standardise-close" and e["CLEAN_TAG"] == "v9-clean" for e in envs)
    assert [e["SRC"] for e in envs] == ["sentas", "sentas", "varsha"] and not any("WATERMARKED" in e for e in envs)


def test_every_gpu_job_is_submitted_for_the_one_attempt_its_reservation_counts(monkeypatch):
    # a job that stops on purpose (photos left to the next run) exits 1; a second Batch attempt would run it again unpaid for
    batch, s3 = _Batch(), _S3()
    gpu.submit(batch, s3, "q", "jd", "v9", {"sentas": [A]}, "R1", [])
    gpu.submit_close(batch, s3, "q", "jd", "v9-clean", {"sentas": [A]}, "R1", [])
    assert len(batch.submitted) == 3 and all(j["retryStrategy"] == {"attempts": 1} for j in batch.submitted)
def test_a_close_only_round_reserves_one_job_per_slice_and_submits_close_jobs(monkeypatch):
    window = 3 * 3600
    assert gpu.worst_seconds(4, window, gpu.CLOSE_ROUND) == 4 * min(C.STANDARDISE_TIMEOUT, window) + 4 * C.GPU_JOB_ALLOWANCE_SECONDS
    assert gpu.worst_seconds(4, window) == 4 * min(C.RETOUCH_TIMEOUT + C.STANDARDISE_TIMEOUT, window) + 8 * C.GPU_JOB_ALLOWANCE_SECONDS
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    monkeypatch.setattr(gpu, "ran_usd", lambda batch, ids, reserved, now, sleep: 0.0)
    batch, s3 = _Batch(), _S3()
    gpu.run_round(batch, s3, "v9-clean", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(100), work=gpu.CLOSE_ROUND)
    assert [j["jobName"] for j in batch.submitted] == [f"{C.JOB_PREFIX}R1-close-sentas-v9-clean-0"]
    assert batch.ceilings == [C.RUN_GPU_VCPUS, C.IDLE_GPU_VCPUS]


def test_a_listed_photo_left_without_a_result_is_a_problem(monkeypatch):
    monkeypatch.setattr(runner.gpu, "run_round", lambda *a: [])
    s = _selection([_listing(A, B)], gpu_round=_round(finished={A}))
    problems, stopped = runner.gpu_round(_Batch(), s, "v9", {"sentas": [A, B]}, "R1", math.inf, sel_mod.Spend(10))
    assert problems == [f"sentas {B[:12]}: no result after the GPU round"] and not stopped


def test_a_gpu_round_stopped_at_the_window_is_a_problem_and_the_run_goes_on(monkeypatch):
    def timed_out(*a):
        raise TimeoutError("GPU jobs not done after 600s")
    monkeypatch.setattr(runner.gpu, "run_round", timed_out)
    s = _selection([_listing(A)], gpu_round=_round(finished={A}))
    assert runner.gpu_round(_Batch(), s, "v9", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(10)) == (
        ["GPU round v9 stopped: GPU jobs not done after 600s"], False)


def test_a_gpu_round_the_budget_cannot_cover_is_skipped_and_the_hosted_steps_go_on(monkeypatch):
    def capped(*a):
        raise sel_mod.SpendCapReached("hosted spend would pass the month's budget")
    monkeypatch.setattr(runner.gpu, "run_round", capped)
    s = _selection([_listing(A)])
    problems, stopped = runner.gpu_round(_Batch(), s, "v9", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(10))
    assert not stopped and problems == ["GPU round v9 not started: hosted spend would pass the month's budget"]


def test_a_gpu_round_with_too_little_window_left_is_skipped_and_the_hosted_steps_go_on(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    batch = _Batch()
    s = _selection([_listing(A)])
    import time
    problems, stopped = runner.gpu_round(batch, s, "v9", {"sentas": [A]}, "R1", time.monotonic() + C.GPU_MIN_WINDOW_SECONDS - 60,
                                         sel_mod.Spend(100))
    assert not stopped and batch.submitted == [] and problems[0].startswith("GPU round v9 not started: only ")


def test_a_gpu_round_that_ended_paid_work_stops_the_run_s_steps(monkeypatch):
    monkeypatch.setattr(runner.gpu, "run_round", lambda *a: (_ for _ in ()).throw(sel_mod.Halted("the work window ended")))
    problems, stopped = runner.gpu_round(_Batch(), _selection([_listing(A)]), "v9", {"sentas": [A]}, "R1", math.inf,
                                         sel_mod.Spend(10))
    assert stopped


def test_a_gpu_round_waits_no_longer_than_the_window_it_reserved_for_however_long_submitting_took(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    untils = []
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: untils.append(until) or [])
    times = iter([400] + [900] * 10)                                               # the round starts at 400; submit ends at 900
    gpu.run_round(_Batch(), _S3(), "v9", {"sentas": [A]}, "R1", 4000, sel_mod.Spend(100),
                  clock=lambda: next(times))
    assert untils == [4000]                                                        # the deadline, as reserved at 400


# --- the owner's clicks first (scraper 1a, 2026-11-06) ----------------------------------------------------------------
# The month's budget once ran out in the backlog's GPU round, then the clean step paid for a click whose cleaned copy
# never got its GPU round or its judge: the owner paid and saw nothing until the next month.
GPU_P, CL_P = "dev/standardise-test/v9/sentas/", "dev/standardise-test/v9-clean/sentas/"
FAIL = json.dumps({"verdict": "fail", "problems": [{"kind": "light_reflection", "where": "x"}],
                   "version": C.JUDGE_VERSION}).encode()


class _Store:
    """A bucket in a dict: what the real select steps read and write."""

    def __init__(self, objects):
        self.objects = dict(objects)

    def get_object(self, Bucket, Key):
        import io
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource, ContentType, MetadataDirective):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _stored_round(prefix, finished=(), judged=(), cleaned=(), slabs=(), checked=()):
    import threading
    r = object.__new__(sel_mod.Round)
    r.prefix, r.finished, r.judged, r.cleaned = prefix, set(finished), set(judged), set(cleaned)
    r.held, r.slabs, r.studio, r.noslab, r.logo_step, r.checked = set(), set(slabs), set(), set(), set(), set(checked)
    r.redraw, r.redrawn = sel_mod.Candidates(prefix, []), set()
    r.attempts, r.edited, r.requested = set(), set(), set()
    r._locks, r._guard = {}, threading.Lock()
    r.asked = r.redraw.asked = set()
    return r


def _clicked_selection(clicked=(("slab-1", "SEN-1", A),), backlog=(("slab-2", "SEN-2", B),), cleaned=(),
                       outline_pending=()):
    """(selection, store) of listings whose one photo failed the judge for a reflection, the `clicked` ones with the
    owner's click (`cleaned`: their copies a run before made, not yet through the GPU; `outline_pending`: whole slabs
    drawn under the older outline rule, so not yet settled), and `backlog` listings whose photo the GPU has not run."""
    listings = [{"key": k, "sku": s, "source": "sentas", "format": "slab", "photos": [p]} for k, s, p in clicked + backlog]
    shas = [p for _, _, p in clicked]
    store = _Store({**{f"dev/products/scraped/sentas/{p}.jpg": b"o" for _, _, p in clicked + backlog},
                    **{f"{GPU_P}{p}.main.jpg": b"m" for p in shas}, **{f"{GPU_P}judged/{p}.json": FAIL for p in shas},
                    **{f"{CL_P}cleaned/{p}.jpg": b"copy" for p in cleaned}})
    s = object.__new__(sel_mod.Selection)
    s.client, s.listings, s.tags, s.exclude, s.root = store, listings, ("v9", "v9-clean"), set(), "dev/standardise-test/v9-clean/"
    s.owner, s.conflicts, s.chosen, s.frozen, s.sources = {}, {}, [], set(), ["sentas"]
    s.clicks = {sku: {"sku": sku, "listing": k, "source": "sentas", "sha": p, "at": "2026-11-06T07:00:00Z"}
                for k, sku, p in clicked}
    whole = set(shas) - set(outline_pending)
    s.rounds = [("v9", {"sentas": _stored_round(GPU_P, finished=shas, judged=shas, slabs=shas, checked=whole)}),
                ("v9-clean", {"sentas": _stored_round(CL_P, cleaned=cleaned)})]
    s.refresh = lambda: None                           # the rounds above are the stores' state, kept up to date by the run
    return s, store


def _clicked_run(monkeypatch, budget, clicked=(("slab-1", "SEN-1", A),), backlog=(("slab-2", "SEN-2", B),),
                 cleaned=(), before=0.0, outline_pending=(), clicks_only=False):
    """A run over _clicked_selection. The GPU round reserves its worst case like the real one, and settles the outline
    of the whole slabs it runs again; a backlog round (listings to standardise) takes all the budget left. (problems,
    report, store, what was paid for edits, the GPU rounds in order, the process spend)."""
    monkeypatch.setattr(C, "CLEAN_ENABLED", False)
    s, store = _clicked_selection(clicked, backlog, cleaned, outline_pending)
    edits, rounds = [], []
    monkeypatch.setattr(sel_mod.clean, "submit", lambda name: edits.append(name) or {"model": name})
    monkeypatch.setattr(sel_mod.clean, "collect", lambda job: b"copy")
    monkeypatch.setattr(sel_mod.judge, "check_clean", lambda job: ({"passes": True}, 0.02))
    monkeypatch.setattr(sel_mod.judge, "judge", lambda job: ({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, 0.02))

    def gpu_round(batch, sel, tag, todo, run_id, deadline, spend):
        rounds.append((tag, todo))
        if not any(todo.values()):
            return [], False
        price = spend.left() if tag == "v9" and backlog else gpu.gpu_usd(gpu.worst_seconds(len(gpu.slices(todo)), math.inf))
        try:
            spend.reserve(price)
        except sel_mod.SpendCapReached as exc:
            return [f"GPU round {tag} not started: {exc}"], False
        spend.settle(price, price)
        rnd, prefix = dict(sel.rounds)[tag]["sentas"], GPU_P if tag == "v9" else CL_P
        for sha in todo.get("sentas", []):
            rnd.finished.add(sha)
            rnd.checked.add(sha)                       # an older outline is drawn again under the current rule
            store.objects[f"{prefix}{sha}.main.jpg"] = b"m"
        for worked_out in ("older_outline", "unchecked", "outdated"):
            rnd.__dict__.pop(worked_out, None)
        return [], False
    monkeypatch.setattr(runner, "gpu_round", gpu_round)
    process = sel_mod.Spend(budget, others=lambda: before)
    problems, report = runner.run(_Batch(), s, process, sel_mod.Spend(10, cap_error=sel_mod.CleanCapReached), "R1",
                                  math.inf, say=lambda m: None, clicks_only=clicks_only)
    return problems, report, store, edits, rounds, process


def test_a_clicks_only_run_finishes_the_clicks_starts_no_other_step_and_merges_its_report(monkeypatch):
    merged = []
    real = runner.select.report
    monkeypatch.setattr(runner.select, "report", lambda sel, waits, marks, merge: merged.append(merge) or
                        real(sel, waits, marks, merge=merge))
    problems, report, store, edits, rounds, process = _clicked_run(monkeypatch, 100.0, backlog=(), clicks_only=True)
    assert edits == ["nano2"] and [tag for tag, _ in rounds] == ["v9-clean"] and merged == [True]
    assert [(r["key"], r["tag"]) for r in report["chosen"]] == [("slab-1", "v9-clean")]


def _finish_price(slices, listings):
    """What finishing the clicks costs the process budget at most: their GPU round's worst case and one judge each."""
    return gpu.gpu_usd(gpu.worst_seconds(slices, math.inf)) + listings * C.JUDGE_PRICE_USD


def test_a_click_is_finished_first_even_when_the_backlog_takes_the_whole_budget(monkeypatch):
    problems, report, store, edits, rounds, process = _clicked_run(monkeypatch, _finish_price(1, 1) + 5)
    assert edits == ["nano2"] and [tag for tag, _ in rounds][:2] == ["v9-clean", "v9"]          # the click before the backlog
    assert [(r["key"], r["tag"], r["sha"]) for r in report["chosen"]] == [("slab-1", "v9-clean", A)]
    assert json.loads(store.objects[f"{CL_P}judged/{A}.json"])["verdict"] == "pass"
    assert process.capped and report["pending"] == ["slab-2"]                                  # the backlog took the rest


def test_a_click_the_month_budget_cannot_finish_is_not_paid_and_waits_named(monkeypatch):
    problems, report, store, edits, rounds, process = _clicked_run(monkeypatch, 10, before=10.0)
    assert edits == [] and not any("/attempts/" in k or "/cleaned/" in k for k in store.objects)
    assert "slab-1: the owner's click waits: month photo budget reached" in "\n".join(problems)
    assert report["chosen"] == [] and not any(A in todo.get("sentas", []) for _, todo in rounds)


def test_a_click_cleaned_before_finishes_with_no_second_edit(monkeypatch):
    problems, report, store, edits, rounds, process = _clicked_run(monkeypatch, _finish_price(1, 1) + 5, cleaned=(A,))
    assert edits == [] and rounds[0] == ("v9-clean", {"sentas": [A]})
    assert [(r["key"], r["tag"]) for r in report["chosen"]] == [("slab-1", "v9-clean")]


def test_of_two_clicks_the_budget_finishes_the_first_and_the_other_waits_unpaid(monkeypatch):
    clicked = (("slab-1", "SEN-1", A), ("slab-3", "SEN-3", D))
    problems, report, store, edits, rounds, process = _clicked_run(
        monkeypatch, _finish_price(1, 1) + C.JUDGE_PRICE_USD / 2, clicked=clicked, backlog=())
    assert edits == ["nano2"] and f"{CL_P}cleaned/{A}.jpg" in store.objects and f"{CL_P}cleaned/{D}.jpg" not in store.objects
    assert [r["key"] for r in report["chosen"]] == ["slab-1"]
    assert "slab-3: the owner's click waits: month photo budget reached" in "\n".join(problems)
    assert not any(p.startswith("slab-1: the owner's click waits") for p in problems)


def _admit(s, left, cap_left=100.0, window=math.inf):
    """(the listing keys admitted, {key: why the others wait}) of the selection's plans."""
    plans, failures = s.plans()
    assert failures == []
    goes, waits = runner.admit(s, plans, left, cap_left, window)
    return [plan.listing["key"] for plan in goes], waits


def _second_photo_cleaned(s, store):
    """slab-1 shows a second photo, E: judged a fail too, its cleaned copy made by an earlier run (an earlier mark)."""
    s.listings[0]["photos"] = [A, E]
    s.rounds[0][1]["sentas"].finished.add(E)
    s.rounds[0][1]["sentas"].judged.add(E)
    store.objects[f"{GPU_P}judged/{E}.json"] = FAIL
    s.rounds[1][1]["sentas"].cleaned.add(E)


def test_a_click_is_new_work_though_another_photo_of_its_listing_has_a_cleaned_copy():
    # review 2026-11-06 (A2.1): the other photo's copy made the click count as finished, and nothing paid it
    s, store = _clicked_selection()
    _second_photo_cleaned(s, store)
    assert _admit(s, 0.0)[0] == []                                                         # it needs an edit: the budget
    assert _admit(s, gpu.price({"sentas": [E, A]}, math.inf))[0] == ["slab-1"]


def test_two_clicked_listings_showing_one_photo_price_its_gpu_work_once(monkeypatch):
    monkeypatch.setattr(C, "RUN_SLICE", 1)
    s, _ = _clicked_selection(clicked=(("slab-1", "SEN-1", A), ("slab-3", "SEN-3", A)), backlog=())
    assert _admit(s, gpu.price({"sentas": [A]}, math.inf)) == (["slab-1", "slab-3"], {})


def test_a_click_s_price_counts_every_copy_its_gpu_round_runs(monkeypatch):
    # the clicks' GPU round runs the listing's older cleaned copy too (clean_todo): its slice is part of the price
    monkeypatch.setattr(C, "RUN_SLICE", 1)
    s, store = _clicked_selection()
    _second_photo_cleaned(s, store)                                                       # E: not yet through the GPU
    both = gpu.price({"sentas": [E, A]}, math.inf)
    assert _admit(s, both - 0.01)[0] == [] and _admit(s, both)[0] == ["slab-1"]


def test_no_new_click_is_paid_when_too_little_window_is_left_for_its_gpu_round():
    s, _ = _clicked_selection()
    assert _admit(s, 100.0, window=C.GPU_MIN_WINDOW_SECONDS - 1)[0] == []
    assert _admit(s, 100.0, window=C.GPU_MIN_WINDOW_SECONDS)[0] == ["slab-1"]


# --- one price, one admission (review 2026-11-06, A2.3, A2.4) --------------------------------------------------------
def _marked(s, store, *keys):
    """The listings `keys` of _clicked_selection fail the judge for a mark on the stone (cleaned without a click)."""
    mark = json.dumps({"verdict": "fail", "problems": [{"kind": "leftover_mark", "where": "x"}],
                       "version": C.JUDGE_VERSION}).encode()
    for l in s.listings:
        if l["key"] in keys:
            store.objects[f"{GPU_P}judged/{l['photos'][0]}.json"] = mark
            s.clicks.pop(l["sku"], None)


def _send_price(plan_models=("nano2", "kontext", "flux3")):
    return sum(C.CLEAN_PRICE_USD[m] + C.JUDGE_PRICE_USD for m in plan_models)


def test_the_clean_step_admits_the_clicks_first_then_the_marks_each_in_sheet_order(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_ENABLED", True)
    s, store = _clicked_selection(clicked=(("slab-1", "SEN-1", A), ("slab-2", "SEN-2", B), ("slab-3", "SEN-3", D)),
                                  backlog=())
    _marked(s, store, "slab-1", "slab-2")                                                   # slab-3 keeps its click
    goes, waits = _admit(s, 100.0, cap_left=2 * _send_price())
    assert goes == ["slab-1", "slab-3"] and waits == {"slab-2": runner.WAIT_CAP}       # the click went before slab-2


def test_the_cleaning_cap_and_the_month_s_budget_each_hold_their_own_line(monkeypatch):
    s, _ = _clicked_selection(clicked=(("slab-1", "SEN-1", A), ("slab-3", "SEN-3", D)), backlog=())
    one = gpu.price({"sentas": [A]}, math.inf)
    assert "month photo budget reached" in _admit(s, one, cap_left=100.0)[1]["slab-3"]
    assert "cleaning cap reached" in _admit(s, 100.0, cap_left=_send_price())[1]["slab-3"]


def test_an_edit_already_paid_for_is_owed_first_and_lowers_what_the_sends_may_take(monkeypatch):
    s, store = _clicked_selection(clicked=(("slab-1", "SEN-1", A), ("slab-3", "SEN-3", D)), backlog=())
    store.objects[f"{CL_P}attempts/{A}.nano2.request.json"] = json.dumps({"model": "nano2"}).encode()
    s.rounds[1][1]["sentas"].requested.add(f"{A}.nano2")                                  # slab-1: paid, not collected
    s.clicks.pop("SEN-1")
    owed = C.JUDGE_PRICE_USD                                                              # its check
    goes, waits = _admit(s, 100.0, cap_left=owed + _send_price() - 0.01)
    assert goes == ["slab-1"] and list(waits) == ["slab-3"]
    assert _admit(s, 100.0, cap_left=owed + _send_price())[0] == ["slab-1", "slab-3"]


def test_the_clean_step_never_spends_past_what_it_admitted_when_a_check_is_lost(monkeypatch):
    s, store = _clicked_selection(backlog=())
    for name in ("nano2", "kontext"):                                                    # flux3 is the model left
        store.objects[f"{CL_P}attempts/{A}.{name}.json"] = json.dumps({"passes": False, "clean_version": C.CLEAN_VERSION}).encode()
        s.rounds[1][1]["sentas"].attempts.add(f"{A}.{name}")
    monkeypatch.setattr(sel_mod.clean, "submit", lambda name: {"model": name})
    monkeypatch.setattr(sel_mod.clean, "collect", lambda job: b"copy")
    monkeypatch.setattr(sel_mod.judge, "check_clean", lambda job: (_ for _ in ()).throw(RuntimeError("the check was lost")))
    plans, _ = s.plans()
    cap = sel_mod.Spend(10, cap_error=sel_mod.CleanCapReached)
    runner.clean_step(s, plans, sel_mod.Spend(100), cap, math.inf, math.inf)
    assert cap.spent <= sel_mod.clean_price(plans[0]) + 1e-9


def test_the_clicks_step_judges_only_what_it_admitted_and_the_copies_it_owes(monkeypatch):
    # review of #540: the clicks' step judged every listing with a click, buying verdicts its admission never priced
    s, store = _clicked_selection(clicked=(("slab-1", "SEN-1", A), ("slab-3", "SEN-3", D)), backlog=())
    s.rounds[0][1]["sentas"].judged.discard(D)                                            # slab-3: not fully judged
    del store.objects[f"{GPU_P}judged/{D}.json"]
    judged, gpu_runs = [], []
    real = runner.select.judge_all
    monkeypatch.setattr(runner.select, "judge_all", lambda sel, spend, deadline: (
        judged.append([l["key"] for l in sel.listings]), real(sel, spend, deadline))[1])
    monkeypatch.setattr(runner, "gpu_round", lambda batch, sel, tag, todo, run_id, deadline, spend: (
        gpu_runs.append((tag, [l["key"] for l in sel.listings])), ([], False))[1])
    monkeypatch.setattr(sel_mod.clean, "submit", lambda name: {"model": name})
    monkeypatch.setattr(sel_mod.clean, "collect", lambda job: b"copy")
    monkeypatch.setattr(sel_mod.judge, "check_clean", lambda job: ({"passes": True}, 0.02))
    monkeypatch.setattr(sel_mod.judge, "judge", lambda job: ({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, 0.02))
    runner.run(_Batch(), s, sel_mod.Spend(100), sel_mod.Spend(10, cap_error=sel_mod.CleanCapReached), "R1", math.inf,
               say=lambda m: None)
    assert judged[0] == ["slab-1"] and gpu_runs[0] == ("v9-clean", ["slab-1"])


def test_a_listing_whose_new_edit_waits_still_collects_the_edit_it_paid_for(monkeypatch):
    s, store = _clicked_selection(backlog=())
    s.listings[0]["photos"] = [A, E]
    s.rounds[0][1]["sentas"].finished.add(E)
    s.rounds[0][1]["sentas"].judged.add(E)
    store.objects[f"{GPU_P}judged/{E}.json"] = FAIL
    store.objects[f"{CL_P}attempts/{E}.nano2.request.json"] = json.dumps({"model": "nano2"}).encode()
    s.rounds[1][1]["sentas"].requested.add(f"{E}.nano2")                                  # E: paid, not collected
    plans, _ = s.plans()
    goes, waits = runner.admit(s, plans, 100.0, C.JUDGE_PRICE_USD, math.inf)              # the cap covers E's check only
    assert waits == {"slab-1": runner.WAIT_CAP}
    assert [(p.listing["key"], [sha for sha, _ in p.collect], p.send) for p in goes] == [("slab-1", [E], ())]


def test_a_new_edit_with_too_little_window_left_waits_for_that_reason():
    s, _ = _clicked_selection()
    assert _admit(s, 100.0, window=C.GPU_MIN_WINDOW_SECONDS - 1)[1] == {"slab-1": runner.WAIT_WINDOW}


def test_the_cap_edge_pays_the_same_listings_whatever_the_threads_do(monkeypatch):
    # review 2026-11-06 (A2.4): the pay step ran listings in parallel up to the cap, so which got the last dollars
    # depended on thread timing
    import random
    import time
    monkeypatch.setattr(C, "CLEAN_ENABLED", True)
    monkeypatch.setattr(C, "SELECT_WORKERS", 4)
    keys = [f"slab-{i}" for i in range(8)]
    shas = [f"{i}" * 64 for i in range(8)]
    paid = []
    for attempt in range(3):
        s, store = _clicked_selection(clicked=tuple((k, f"SEN-{i}", sha) for i, (k, sha) in enumerate(zip(keys, shas))),
                                      backlog=())
        _marked(s, store, *keys)
        monkeypatch.setattr(sel_mod.clean, "submit", lambda name: time.sleep(random.random() / 50) or {"model": name})
        monkeypatch.setattr(sel_mod.clean, "collect", lambda job: b"copy")
        monkeypatch.setattr(sel_mod.judge, "check_clean", lambda job: ({"passes": True}, 0.02))
        cap = sel_mod.Spend(3 * _send_price(), cap_error=sel_mod.CleanCapReached)
        plans, _ = s.plans()
        problems, _, _, waits, marks = runner.clean_step(s, plans, sel_mod.Spend(100), cap, math.inf, math.inf)
        paid.append(sorted(k for k in store.objects if "/cleaned/" in k))
        assert cap.spent <= cap.budget and not cap.capped
        assert problems == [f"the marks of 5 listings wait: {runner.WAIT_CAP}"] and waits == {}   # no click waits
        assert marks == {k: runner.WAIT_CAP for k in keys[3:]}       # each mark's wait by listing, for the review list
    assert paid[0] == paid[1] == paid[2] == sorted(f"{CL_P}cleaned/{sha}.jpg" for sha in shas[:3])


def test_a_click_blocked_by_its_outline_at_the_start_is_paid_in_the_run_s_own_clean_step(monkeypatch):
    # dev run 20261106082352439 (MAR-P/24/04): the clicked whole slab awaited its outline recheck when the run began, so
    # the clicks' step could not clean it; the run's GPU round settled the outline, and its own clean step pays it
    problems, report, store, edits, rounds, process = _clicked_run(monkeypatch, 100.0, backlog=(), outline_pending=(A,))
    assert edits == ["nano2"] and [(r["key"], r["tag"]) for r in report["chosen"]] == [("slab-1", "v9-clean")]
    assert [tag for tag, todo in rounds if any(todo.values())] == ["v9", "v9-clean"]        # settled, then its copy
    assert not any("slab-1" in p for p in problems)


def test_a_click_still_awaiting_its_outline_at_the_end_of_a_run_is_named_blocked(monkeypatch):
    # dev run 20261106082352439: the click was counted "finished" with nothing paid and no line naming it
    problems, report, store, edits, rounds, process = _clicked_run(monkeypatch, 10, backlog=(), outline_pending=(A,),
                                                                   before=10.0)
    assert edits == [] and report["chosen"] == []
    assert any(p.startswith(f"slab-1: owner's click on {A[:12]}: blocked: the listing is not fully judged") for p in problems)


def test_a_mark_that_waits_beside_a_collected_click_is_never_called_the_click_s_wait(monkeypatch):
    monkeypatch.setattr(C, "CLEAN_ENABLED", True)
    s, store = _clicked_selection(backlog=())
    s.listings[0]["photos"] = [A, E]
    s.rounds[0][1]["sentas"].finished.add(E)
    s.rounds[0][1]["sentas"].judged.add(E)
    store.objects[f"{GPU_P}judged/{E}.json"] = json.dumps({"verdict": "fail", "problems": [{"kind": "leftover_mark"}],
                                                           "version": C.JUDGE_VERSION}).encode()
    for name in sel_mod.clean.MODELS:                                                    # A: paid, never checked
        store.objects[f"{CL_P}attempts/{A}.{name}.request.json"] = json.dumps({"model": name}).encode()
        s.rounds[1][1]["sentas"].requested.add(f"{A}.{name}")
    monkeypatch.setattr(sel_mod.clean, "collect", lambda job: b"copy")
    monkeypatch.setattr(sel_mod.judge, "check_clean", lambda job: ({"passes": False}, 0.02))
    plans, _ = s.plans()
    cap = sel_mod.Spend(3 * C.JUDGE_PRICE_USD, cap_error=sel_mod.CleanCapReached)          # A's checks, not E's edit
    problems, _, _, waits, marks = runner.clean_step(s, plans, sel_mod.Spend(100), cap, math.inf, math.inf)
    assert waits == {} and not any("owner's click waits" in p for p in problems)
    assert any(p.startswith("the marks of 1 listings wait") for p in problems) and marks == {"slab-1": runner.WAIT_CAP}


def test_without_a_cleaning_cap_a_ready_click_says_it_waits_for_one(monkeypatch):
    s, store = _clicked_selection(backlog=())
    monkeypatch.setattr(runner, "gpu_round", lambda batch, sel, tag, todo, run_id, deadline, spend: ([], False))
    problems, report = runner.run(_Batch(), s, sel_mod.Spend(100), None, "R1", math.inf, say=lambda m: None)
    assert f"owner's click on {A[:12]}: waiting: no cleaning cap set" in report["tracked"][0]["what_happened"]


def test_a_month_with_money_to_judge_but_none_for_a_round_judges_what_is_finished(monkeypatch):
    # review 2026-11-06: a round that cannot start ends nothing; the judge, the report and publish still run
    _gpu_env(monkeypatch)
    s, store = _clicked_selection()
    s.clicks = {}
    s.rounds[0][1]["sentas"].judged.discard(A)
    del store.objects[f"{GPU_P}judged/{A}.json"]
    monkeypatch.setattr(sel_mod.judge, "judge", lambda job: ({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, 0.02))
    batch = _Batch()
    problems, report = runner.run(batch, s, sel_mod.Spend(0.5), None, "R1", math.inf, say=lambda m: None)
    assert batch.submitted == [] and [r["key"] for r in report["chosen"]] == ["slab-1"]
    assert any("GPU round v9 not started" in p for p in problems)


# --- the GPU's share of the month's budget ----------------------------------------------------------------------------
def _gpu_env(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "jd")
    monkeypatch.setattr(C, "GPU_VCPU_HOUR_USD", 0.25)
    monkeypatch.setattr(C, "GPU_JOB_VCPUS", 4)                                      # 1.00 USD per GPU-hour
    monkeypatch.setattr(C, "GPU_JOB_ALLOWANCE_SECONDS", 720)                        # 0.20 USD per job


def test_a_round_reserves_its_worst_case_one_gpu_per_slice_within_the_window_plus_every_job_s_allowance(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(C, "RUN_GPU_VCPUS", 8)                                      # 2 GPUs at once
    longest, extra = C.RETOUCH_TIMEOUT + C.STANDARDISE_TIMEOUT, 2 * 720              # a slice's two jobs
    assert gpu.worst_seconds(1, 10 * longest) == longest + extra                   # one slice: its two jobs, in turn
    assert gpu.worst_seconds(1, 600) == 600 + extra                                # never past the window
    assert gpu.worst_seconds(5, 600) == 2 * 600 + 5 * extra                        # 2 GPUs at once; all 10 jobs may start
    assert gpu.gpu_usd(3600) == pytest.approx(1.0)


def test_a_round_never_settles_above_what_it_reserved(monkeypatch):
    # more slices than run at once, every job started, the pool busy the whole window: the most a round can cost
    _gpu_env(monkeypatch)
    monkeypatch.setattr(C, "RUN_SLICE", 1)
    monkeypatch.setattr(C, "RUN_GPU_VCPUS", 8)                                      # 2 GPUs at once
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: [])
    window = C.GPU_MIN_WINDOW_SECONDS
    batch, kept = _Batch(), []
    jobs = [f"job{i}" for i in range(1, 11)]                                       # 5 slices, 10 jobs
    busy_ms = 2 * window * 1000 // len(jobs)                                        # 2 GPUs x the window, shared
    batch.times = {j: {"startedAt": i * busy_ms, "stoppedAt": (i + 1) * busy_ms} for i, j in enumerate(jobs)}
    spend = sel_mod.Spend(1000, save=lambda total, uses: kept.append(total))
    photos = [c * 64 for c in "12345"]
    gpu.run_round(batch, _S3(), "v9", {"sentas": photos}, "R1", window, spend, clock=lambda: 0)
    assert spend.spent <= kept[0] + 1e-9


def test_a_round_settles_what_its_jobs_ran_plus_each_job_s_allowance(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: [])
    batch = _Batch()
    batch.times = {"job1": {"startedAt": 0, "stoppedAt": 1_800_000}, "job2": {"startedAt": 1_800_000, "stoppedAt": 5_400_000}}
    kept = []
    spend = sel_mod.Spend(100, save=lambda total, uses: kept.append(total))
    gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, "R1", 7200, spend, clock=lambda: 0)
    assert kept[0] == pytest.approx(2.4)                                           # reserved: the 2 h window + 2 allowances
    assert spend.spent == pytest.approx(1.9) and spend.reserved == 0               # ran 0.5 h + 1 h, + 2 allowances


def test_a_round_is_sized_to_the_budget_left(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(C, "RUN_SLICE", 1)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: [])
    batch, kept = _Batch(), []
    spend = sel_mod.Spend(5.1, save=lambda total, uses: kept.append(total))      # 2 slices of 2.40 and 2 verdicts fit, not 3
    gpu.run_round(batch, _S3(), "v9", {"sentas": [A, B, D]}, "R1", 7200, spend, clock=lambda: 0)
    assert len(batch.submitted) == 4 and kept[0] == pytest.approx(4.8) and not spend.capped


def test_a_round_leaves_the_judging_of_its_results_covered(monkeypatch):
    # review 2026-11-06 (A2.3): a round sized to the GPU alone spent the month on results it could not then judge
    _gpu_env(monkeypatch)
    monkeypatch.setattr(C, "RUN_SLICE", 1)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: [])
    batch, kept = _Batch(), []
    spend = sel_mod.Spend(4.9, save=lambda total, uses: kept.append(total))   # 2 slices' GPU (4.80) fit, not their verdicts
    gpu.run_round(batch, _S3(), "v9", {"sentas": [A, B, D]}, "R1", 7200, spend, clock=lambda: 0)
    assert len(batch.submitted) == 2 and kept[0] == pytest.approx(2.4)             # 1 slice; its verdict reserved later


def test_a_round_whose_one_slice_cannot_also_be_judged_submits_nothing(monkeypatch):
    _gpu_env(monkeypatch)
    batch, spend = _Batch(), sel_mod.Spend(2.45)                                     # the GPU (2.40), not its verdict too
    with pytest.raises(sel_mod.SpendCapReached):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, "R1", 7200, spend, clock=lambda: 0)
    assert batch.submitted == [] and spend.capped and spend.reserved == 0


def test_one_price_for_gpu_work_and_a_close_up_round_has_no_verdict_in_it(monkeypatch):
    _gpu_env(monkeypatch)
    todo = {"sentas": [A, B]}
    assert gpu.price(todo, 7200) == pytest.approx(gpu.gpu_usd(gpu.worst_seconds(1, 7200)) + 2 * C.JUDGE_PRICE_USD)
    assert gpu.price(todo, 7200, gpu.CLOSE_ROUND) == pytest.approx(gpu.gpu_usd(gpu.worst_seconds(1, 7200, gpu.CLOSE_ROUND)))


def test_a_round_the_budget_cannot_cover_even_one_slice_of_submits_nothing(monkeypatch):
    _gpu_env(monkeypatch)
    batch, spend = _Batch(), sel_mod.Spend(1.0)
    with pytest.raises(sel_mod.SpendCapReached):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, "R1", 7200, spend, clock=lambda: 0)
    assert batch.submitted == [] and batch.ceilings == [] and spend.capped


def _stopping(batch, polls):
    """describe_jobs answers in turn: job1 as given per poll, job2 never started."""
    answers = iter(polls)

    def describe(jobs):
        job1 = next(answers)
        return {"jobs": [{"jobId": "job1", "jobName": "job1", **job1}, {"jobId": "job2", "jobName": "job2", "status": "FAILED"}]}
    batch.describe_jobs = describe
    batch.terminate_job = lambda jobId, reason: None


def test_a_stopped_round_reads_its_jobs_times_once_they_have_stopped(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: (_ for _ in ()).throw(SystemExit("stopped by signal 15")))
    batch, slept = _Batch(), []
    _stopping(batch, [{"status": "RUNNING", "startedAt": 0}, {"status": "FAILED", "startedAt": 0, "stoppedAt": 1_800_000}])
    spend = sel_mod.Spend(100)
    with pytest.raises(SystemExit):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, "R1", 7200, spend, clock=lambda: 0,
                      now=lambda: 99_999, sleep=slept.append)
    assert spend.spent == pytest.approx(0.7) and spend.reserved == 0               # 0.5 h + its allowance, not to now
    assert slept == [C.STOP_POLL_SECONDS]


def test_a_job_still_not_stopped_after_the_wait_counts_to_now(monkeypatch):
    _gpu_env(monkeypatch)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: (_ for _ in ()).throw(SystemExit("stopped by signal 15")))
    batch, tries = _Batch(), C.STOP_WAIT_SECONDS // C.STOP_POLL_SECONDS
    _stopping(batch, [{"status": "RUNNING", "startedAt": 0}] * (tries + 1))
    spend = sel_mod.Spend(100)
    with pytest.raises(SystemExit):
        gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, "R1", 7200, spend, clock=lambda: 0,
                      now=lambda: 900, sleep=lambda s: None)
    assert spend.spent == pytest.approx(0.25 + 0.2)                               # job2 never started: no allowance


def test_when_batch_cannot_say_what_ran_the_round_counts_what_it_reserved(monkeypatch):
    from botocore.exceptions import ClientError
    _gpu_env(monkeypatch)
    monkeypatch.setattr(gpu, "wait", lambda batch, ids, until, clock: [])
    batch = _Batch()
    batch.describe_jobs = lambda jobs: (_ for _ in ()).throw(ClientError({"Error": {"Code": "ServerException"}}, "DescribeJobs"))
    spend = sel_mod.Spend(100)
    gpu.run_round(batch, _S3(), "v9", {"sentas": [A]}, "R1", 7200, spend, clock=lambda: 0)
    assert spend.spent == pytest.approx(2.4)


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
    assert sorted(stopped) == ["old1", "old3"] and batch.ceilings == [C.IDLE_GPU_VCPUS]
    assert asked[0]["filters"] == [{"name": "JOB_NAME", "values": [f"{C.JOB_PREFIX}*"]}] and asked[1]["nextToken"] == "t"


def test_a_paid_hosted_step_runs_once_and_its_failures_wait_for_the_next_run():
    # review of #540: a second pass paid again for what failed after it was sent (a check that was lost), past what the
    # run had admitted; every hosted step pays, so each runs once and the next run retries from what is stored
    calls = []

    def flaky(sel, spend, deadline):
        calls.append(1)
        return [("slab-1", "timeout")], False
    assert runner.hosted(flaky, None, None, math.inf) == (["slab-1: timeout"], False) and len(calls) == 1


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


def test_the_lock_holder_is_the_run_id_its_body_names_and_none_without_a_lock():
    # the config API's photo routes answer 409 with it, so the page can say which run to wait for
    import io
    from botocore.exceptions import ClientError

    class _Body:
        def __init__(self, body):
            self.body = body

        def get_object(self, Bucket, Key):
            if self.body is None:
                raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
            assert Key == runner.LOCK
            return {"Body": io.BytesIO(self.body)}
    assert runner.lock_holder(_Body(json.dumps({"run_id": "20261107T100000Z", "tags": []}).encode())) == "20261107T100000Z"
    assert runner.lock_holder(_Body(None)) is None


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


def test_a_passing_storage_error_or_a_concurrent_write_never_loses_the_lock():
    import time
    from botocore.exceptions import ClientError, EndpointConnectionError
    s3, lost = _LockS3(), []
    put, hiccups = s3.put_object, iter([EndpointConnectionError(endpoint_url="https://s3"),
                                        ClientError({"Error": {"Code": "ConditionalRequestConflict"}}, "PutObject")])

    def flaky(**kw):
        if "IfMatch" in kw:                                         # a heartbeat
            failure = next(hiccups, None)
            if failure is not None:
                raise failure
        return put(**kw)
    s3.put_object = flaky
    with runner.exclusive(s3, {"run_id": "R1"}, on_lost=lambda: lost.append(1), beat=0.02):
        time.sleep(0.2)
        assert s3.locks[runner.LOCK]["owner"] == "R1" and s3.writes >= 3            # refreshed on past both hiccups
    assert lost == [] and runner.LOCK not in s3.keys                               # still ours, released


def test_a_lock_gone_from_under_the_heartbeat_is_lost_not_a_passing_error():
    import time
    from botocore.exceptions import ClientError
    s3, lost = _LockS3(), []
    put = s3.put_object

    def missing(**kw):
        if "IfMatch" in kw:                                         # S3 answers a conditional write to a deleted key 404
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "PutObject")
        return put(**kw)
    with runner.exclusive(s3, {"run_id": "R1"}, on_lost=lambda: lost.append(1), beat=0.02):
        s3.put_object = missing
        s3.locks.pop(runner.LOCK)
        time.sleep(0.1)
    assert lost == [1]


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


def _idle_ceiling_read_by_settings(value):
    """IDLE_GPU_VCPUS as a fresh interpreter's settings read it, with SCRAPER_STANDARDISE_IDLE_GPU_VCPUS set to value (None:
    unset). A subprocess, because settings reads the environment once, at import."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if not k.endswith("STANDARDISE_IDLE_GPU_VCPUS")}
    if value is not None:
        env["SCRAPER_STANDARDISE_IDLE_GPU_VCPUS"] = value
    out = subprocess.run([sys.executable, "-c", "from stone_pipeline.config.settings import STANDARDISE as C; "
                          "print(C.IDLE_GPU_VCPUS)"], env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_the_idle_ceiling_is_the_one_terraform_gives_the_pool_and_has_no_default_of_its_own():
    assert _idle_ceiling_read_by_settings("12") == "12"
    assert _idle_ceiling_read_by_settings(None) == "None"


def test_a_host_without_an_idle_ceiling_fails_before_it_touches_the_pool(monkeypatch):
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")
    monkeypatch.setattr(C, "IDLE_GPU_VCPUS", None)
    batch = _Batch()
    with pytest.raises(KeyError, match="SCRAPER_STANDARDISE_IDLE_GPU_VCPUS"):
        with gpu.raised(batch, "gpu-pool"):
            pass
    with pytest.raises(KeyError, match="SCRAPER_STANDARDISE_IDLE_GPU_VCPUS"):
        gpu.set_idle(batch)
    assert batch.ceilings == []                                                     # never raised, never guessed


# --- open books made before their halves had a slab -----------------------------------------------------------------
def test_an_open_book_made_before_its_halves_had_a_slab_goes_to_the_gpu_again_once():
    old, done, tried, close_up = A, B, D, E
    gpu_round = _round(finished={old, done, tried, close_up}, studio={old, done, tried}, slabs={done}, noslab={tried})
    s = _selection([_listing(old, done, tried, key="one"), _listing(close_up, key="two")], gpu_round=gpu_round,
                   clean_round=_round(finished={old}, cleaned={old}, studio={old}))
    assert s.gpu_todo() == {"sentas": [old]}                       # a side view and no slab, never completed before
    assert s.clean_todo() == {"sentas": [old]}


def test_a_batch_error_in_a_round_is_a_problem_and_the_run_goes_on(monkeypatch):
    # review 2026-11-06 (A2.7): a Batch ClientError ended the run before its report, so nothing finished was published
    from botocore.exceptions import ClientError
    refused = ClientError({"Error": {"Code": "ClientException", "Message": "queue disabled"}}, "SubmitJob")
    monkeypatch.setattr(runner.gpu, "run_round", lambda *a: (_ for _ in ()).throw(refused))
    s = _selection([_listing(A)])
    problems, stopped = runner.gpu_round(_Batch(), s, "v9", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(10))
    assert not stopped and problems[0].startswith("GPU round v9 failed: ") and "queue disabled" in problems[0]


def test_an_open_book_still_without_its_slab_after_the_round_is_a_problem(monkeypatch):
    monkeypatch.setattr(runner.gpu, "run_round", lambda *a: [])
    s = _selection([_listing(A)], gpu_round=_round(finished={A}, studio={A}))
    problems, _ = runner.gpu_round(_Batch(), s, "v9", {"sentas": [A]}, "R1", math.inf, sel_mod.Spend(10))
    assert problems == [f"sentas {A[:12]}: no result after the GPU round"]


def test_a_round_s_outdated_results_are_worked_out_once_not_per_candidate():
    r = sel_mod.Round.__new__(sel_mod.Round)
    r.finished, r.studio, r.slabs, r.noslab, r.held, r.logo_step, r.checked = {A, B}, {A, B}, {B}, set(), set(), set(), {B}
    r.redrawn = set()
    assert r.outdated == {A} and r.outdated is r.outdated


def test_an_open_book_the_retired_logo_step_made_is_never_completed():
    # its completion would publish a close view and 3D slab from the photo with the logo, unjudged
    rnd = _round(finished={A, B}, studio={A, B}, logo_step={A})
    assert rnd.outdated == {B}


def test_a_whole_slab_made_under_the_older_outline_rule_goes_to_the_gpu_once():
    old, current, close_up = A, B, D
    gpu_round = _round(finished={old, current, close_up}, slabs={old, current}, checked={current})
    s = _selection([_listing(old, current, close_up)], gpu_round=gpu_round,
                   clean_round=_round(finished={old, current}, cleaned={old, current}, slabs={old, current}, checked={current}))
    assert s.gpu_todo() == {"sentas": [old]} and s.clean_todo() == {"sentas": [old]}


def test_a_whole_slab_the_retired_logo_step_made_is_never_rechecked():
    # its re-run would start from the photo with its logo and redraw views no judge saw
    rnd = _round(finished={A, B}, slabs={A, B}, checked=(), logo_step={A})
    assert rnd.older_outline == {B} and rnd.outdated == {B}


def test_a_chosen_whole_slab_made_under_the_older_outline_rule_goes_to_the_gpu_without_its_listing_being_picked():
    gpu_round = _round(finished={A, B, D}, slabs={A, B, D}, checked={B})
    s = _selection([], gpu_round=gpu_round, chosen=[("v9", "sentas", A), ("v9", "sentas", B), ("v9-clean", "sentas", D)],
                   exclude={E})
    assert s.gpu_todo() == {"sentas": [A]}                         # B is current; D is the cleaned run's (clean_todo)
    s = _selection([], gpu_round=_round(finished={E}, slabs={E}), chosen=[("v9", "sentas", E)], exclude={E})
    assert s.gpu_todo() == {}                                      # an excluded photo is unpublished, never redrawn


def test_only_the_cleaned_copies_of_the_run_s_listings_and_the_chosen_ones_go_to_the_gpu():
    F, G = "f" * 64, "9" * 64
    cleaned = _round(cleaned={A, B, D, E, F, G})                    # none run yet
    s = _selection([_listing(A, F)], clean_round=cleaned, exclude={F},
                   chosen=[("v9-clean", "sentas", B), ("v9", "sentas", D)])
    assert s.clean_todo() == {"sentas": [A, B]}    # D: its choice is the scraped run's; E, G: no current listing; F: excluded
