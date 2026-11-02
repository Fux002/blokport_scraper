"""The standardised-photo process (STANDARDISE_PROCESS.md), with AWS stand-ins: it picks exactly the listings without a
valid chosen photo, continues in the tags the published results came from, keeps one hosted budget per month across
runs, publishes only what changed, links the new photos in the same run only when the published folder changed, and runs
after a produce on the owner's button as the runner's photos stage."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys

import pytest
from botocore.exceptions import ClientError

from deploy import standardise_publish as pub
from deploy import standardise_run as proc
from deploy import standardise_select as select
from stone_pipeline.config import runner
from stone_pipeline.config.settings import ENV_SEGMENT, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.ledger import writethrough

A, B, D, E = "a" * 64, "b" * 64, "d" * 64, "e" * 64
RUN = f"{ENV_SEGMENT}/standardise-test/{C.RUN_TAG}/sentas/"
ROOT = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/"


class _S3:
    """A bucket in a dict: ETags as S3 gives a single-part object (the md5 of its bytes, kept by a server-side copy),
    listing, and every write recorded."""

    def __init__(self, objects=None):
        self.objects, self.written = {}, []
        for key, body in (objects or {}).items():
            self._store(key, body)

    def _store(self, key, body, metadata=None):
        self.objects[key] = {"Body": body, "ETag": f'"{hashlib.md5(body).hexdigest()}"', "Metadata": metadata or {}}

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None, **condition):
        self.written.append(Key)
        self._store(Key, Body, Metadata)
        return {"ETag": self.objects[Key]["ETag"]}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]["Body"]), "ETag": self.objects[Key]["ETag"]}

    def head_object(self, Bucket, Key):
        return {"ETag": self.objects[Key]["ETag"], "Metadata": self.objects[Key]["Metadata"]}

    def copy_object(self, Bucket, Key, CopySource, ContentType, MetadataDirective):
        self.written.append(Key)
        self.objects[Key] = dict(self.objects[CopySource["Key"]], Metadata={})

    def delete_object(self, Bucket, Key, **condition):
        self.written.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k, "ETag": v["ETag"]} for k, v in sorted(objects.items()) if k.startswith(Prefix)]}]
        return _Pages()


def _listing(key, *photos, fmt="slab"):
    return {"key": key, "source": "sentas", "format": fmt, "photos": list(photos)}


def _record(key, sha, prefix=RUN):
    return json.dumps({"source": "sentas", "key": key, "sha": sha, "tag": C.RUN_TAG, "prefix": prefix}).encode()


def _rec(sha):
    return {"sha": sha, "tag": C.RUN_TAG, "prefix": RUN}


# --- what a run works on ----------------------------------------------------------------------------------------------
def test_the_process_picks_exactly_the_listings_without_a_valid_chosen_photo():
    listings = [_listing("done", A, B), _listing("new", D), _listing("moved", E), _listing("excluded", B)]
    records = {"done": _rec(A), "moved": _rec(D), "excluded": _rec(B), "gone": _rec(E)}
    assert [l["key"] for l in proc.todo(listings, records, {B}, {f"{RUN}{A}.slab.png"})] == ["new", "moved", "excluded"]


def test_a_slab_listing_whose_chosen_photo_has_no_3d_slab_is_picked_again_with_its_choice_kept():
    listings = [_listing("whole", A, B), _listing("close-up", D, E), _listing("block", E, fmt="block"),
                _listing("tile", E, fmt="tile")]
    records = {"whole": _rec(A), "close-up": _rec(D), "block": _rec(E), "tile": _rec(E)}
    found = proc.todo(listings, records, set(), {f"{RUN}{A}.slab.png"})
    assert found == [{**_listing("close-up", D, E), "kept": (C.RUN_TAG, D)}]          # a block or tile has no 3D slab


def test_which_chosen_photos_have_a_3d_slab_is_read_from_the_runs_the_records_point_at():
    other = f"{ENV_SEGMENT}/standardise-test/{C.CLEAN_TAG}/sentas/"
    s3 = _S3({f"{RUN}{A}.slab.png": b"p", f"{RUN}{A}.main.jpg": b"m", f"{RUN}held/{B}.json": b"{}",
              f"{other}{B}.slab.png": b"p", f"{ENV_SEGMENT}/standardise-test/v1/sentas/{D}.slab.png": b"p"})
    records = {"one": {"sha": A, "prefix": RUN}, "two": {"sha": B, "prefix": other}}
    assert proc.whole_slabs(s3, records) == {f"{RUN}{A}.slab.png", f"{other}{B}.slab.png"}


def test_the_choice_records_are_read_from_the_clean_root():
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{ROOT}tracked.csv": b"source\n", f"{ROOT}sentas/x.json": b"{}"})
    assert proc.choices(s3, ROOT) == {"one": json.loads(_record("one", A))}


def test_the_process_continues_in_the_tags_the_published_results_came_from():
    assert C.RUN_TAG == "v2" and C.CLEAN_TAGS == {"dev": "v2-clean", "prod": "v2-clean2"}
    assert C.CLEAN_TAG == C.CLEAN_TAGS[ENV_SEGMENT]


# --- one budget per month ---------------------------------------------------------------------------------------------
def _spent(s3, month, run_id, usd):
    s3._store(f"{proc.spend_prefix(month)}{run_id}.json", json.dumps({"usd": usd}).encode())


def test_the_month_s_budget_counts_every_run_of_the_month(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 1.0)
    s3 = _S3()
    _spent(s3, "2026-11", "20261101T000000Z", 0.5)
    _spent(s3, "2026-11", "20261101T060000Z", 0.4)
    spend = proc.month_spend(s3, "20261102T120000Z")
    with pytest.raises(select.SpendCapReached):
        spend.reserve(C.JUDGE_PRICE_USD)                                     # 0.90 + 0.14 would pass 1.00
    assert spend.capped


def test_a_new_month_has_its_whole_budget(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 1.0)
    s3 = _S3()
    _spent(s3, "2026-10", "20261031T230000Z", 1.0)
    proc.month_spend(s3, "20261101T000100Z").reserve(0.5)


def test_each_run_keeps_only_its_own_spend_a_reserve_counting_until_it_is_settled(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 10.0)
    run_id, other = "20261102T120000Z", "20261101T000000Z"
    s3 = _S3()
    _spent(s3, "2026-11", other, 2.0)
    spend = proc.month_spend(s3, run_id)
    own = f"{proc.spend_prefix('2026-11')}{run_id}.json"
    spend.reserve(0.14)
    assert json.loads(s3.objects[own]["Body"])["usd"] == pytest.approx(0.14)        # a run that dies now still counts it
    spend.settle(0.14, 0.02)
    assert json.loads(s3.objects[own]["Body"])["usd"] == pytest.approx(0.02)
    assert json.loads(s3.objects[f"{proc.spend_prefix('2026-11')}{other}.json"]["Body"])["usd"] == 2.0   # never rewritten


def test_the_month_is_summed_again_when_a_call_is_settled(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 3.0)
    s3 = _S3()
    spend = proc.month_spend(s3, "20261102T120000Z")
    _spent(s3, "2026-11", "20261102T110000Z", 2.9)                           # written by another run since
    spend.reserve(0.05)
    spend.settle(0.05, 0.05)
    assert spend.before == pytest.approx(2.9)
    with pytest.raises(select.SpendCapReached):
        spend.reserve(0.1)


def _configured(monkeypatch):
    for name, value in (("SCRAPER_GPU_QUEUE", "q"), ("SCRAPER_STANDARDISE_JOBDEF", "jd"), ("FAL_KEY", "k")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(C, "BUDGET_USD", 50.0)
    monkeypatch.setattr(proc, "RUN_TIMEOUT_SECONDS", C.WORK_SECONDS + C.FINISH_SECONDS + 1)


def test_without_a_budget_the_process_refuses_to_start(monkeypatch):
    _configured(monkeypatch)
    monkeypatch.setattr(C, "BUDGET_USD", None)
    assert proc.unconfigured() == ["SCRAPER_STANDARDISE_BUDGET_USD"]
    assert proc.main() == 2


def test_the_gpu_target_and_the_hosted_key_must_be_configured(monkeypatch):
    _configured(monkeypatch)
    for name in ("SCRAPER_GPU_QUEUE", "BLOKPORT_GPU_QUEUE", "SCRAPER_STANDARDISE_JOBDEF", "BLOKPORT_STANDARDISE_JOBDEF", "FAL_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert proc.unconfigured() == ["SCRAPER_GPU_QUEUE", "SCRAPER_STANDARDISE_JOBDEF", "FAL_KEY"]


def test_the_work_window_and_the_finish_must_fit_the_runner_s_ceiling(monkeypatch):
    _configured(monkeypatch)
    assert proc.unconfigured() == []
    monkeypatch.setattr(proc, "RUN_TIMEOUT_SECONDS", C.WORK_SECONDS + C.FINISH_SECONDS)
    assert proc.unconfigured() == [f"SCRAPER_STANDARDISE_WORK_SECONDS ({C.WORK_SECONDS}) + FINISH_SECONDS ({C.FINISH_SECONDS}) "
                                   f"under SCRAPER_RUN_TIMEOUT_SECONDS ({C.WORK_SECONDS + C.FINISH_SECONDS})"]


# --- publish: only what changed ---------------------------------------------------------------------------------------
def _published_bucket():
    return _S3({f"{RUN}{A}.main.jpg": b"m", f"{RUN}{A}.white.jpg": b"w", f"{ROOT}chosen/one.json": _record("one", A)})


def test_publish_copies_only_what_changed_and_says_whether_the_folder_changed():
    s3 = _published_bucket()
    first = pub.publish(s3, ROOT, say=lambda m: None)
    assert first["copied"] == 2 and first["changed"]
    assert s3.objects[imagestore.standard_key("sentas", A, "main")]["Body"] == b"m"
    s3.written.clear()
    second = pub.publish(s3, ROOT, say=lambda m: None)
    assert not second["changed"] and s3.written == []                      # nothing rewritten: no new versions, no re-serve


def test_a_view_its_run_made_again_is_published_again():
    s3 = _published_bucket()
    pub.publish(s3, ROOT, say=lambda m: None)
    s3._store(f"{RUN}{A}.main.jpg", b"m2")
    again = pub.publish(s3, ROOT, say=lambda m: None)
    assert again["copied"] == 1 and again["changed"]
    assert s3.objects[imagestore.standard_key("sentas", A, "main")]["Body"] == b"m2"


def test_a_view_no_longer_chosen_is_removed_and_that_is_a_change():
    s3 = _published_bucket()
    s3._store(imagestore.standard_key("sentas", B, "main"), b"old")
    out = pub.publish(s3, ROOT, say=lambda m: None)
    assert out["removed"] == 1 and imagestore.standard_key("sentas", B, "main") not in s3.objects


def test_3d_slabs_are_cut_out_a_few_at_a_time_on_the_small_service(monkeypatch):
    s3 = _published_bucket()
    s3._store(f"{RUN}{A}.slab.png", b"png")
    pools = []
    real = pub.ThreadPoolExecutor
    monkeypatch.setattr(pub, "ThreadPoolExecutor", lambda n: pools.append(n) or real(n))
    monkeypatch.setattr(pub, "_publish_cutout", lambda client, s, key, etag: True)
    assert pub.publish(s3, ROOT, say=lambda m: None)["made_3d"] == 1
    assert C.CUTOUT_WORKERS in pools and C.CUTOUT_WORKERS <= 4


def _slab_png():
    import cv2
    import numpy as np
    ok, buf = cv2.imencode(".png", np.dstack([np.full((40, 60, 3), 120, np.uint8), np.full((40, 60), 255, np.uint8)]))
    assert ok
    return buf.tobytes()


def test_a_choice_moved_from_a_close_up_to_a_whole_slab_publishes_its_views_and_its_3d_slab_and_drops_the_old(monkeypatch):
    s3 = _S3({f"{RUN}{A}.main.jpg": b"ma", f"{RUN}{A}.white.jpg": b"wa", f"{ROOT}chosen/one.json": _record("one", A),
              **{f"{RUN}{B}.{v}.jpg": f"{v}b".encode() for v in imagestore.STANDARD_VIEWS}, f"{RUN}{B}.slab.png": _slab_png()})
    first = pub.publish(s3, ROOT, say=lambda m: None)
    assert first["copied"] == 2 and first["made_3d"] == 0                              # the close-up: two views, no 3D
    s3._store(f"{ROOT}chosen/one.json", _record("one", B))
    moved = pub.publish(s3, ROOT, say=lambda m: None)
    assert moved["changed"] and moved["copied"] == 4 and moved["made_3d"] == 1 and moved["removed"] == 2
    assert {k for k in s3.objects if "/products/standard/" in k} == (
        {imagestore.standard_key("sentas", B, v) for v in imagestore.STANDARD_VIEWS}
        | {imagestore.cutout_key("sentas", B, ext) for ext in ("webp", "json")})


def test_publish_without_any_choice_record_is_refused_with_its_own_error():
    with pytest.raises(pub.NoChoiceRecords):
        pub.publish(_S3({f"{RUN}{A}.main.jpg": b"m"}), ROOT, say=lambda m: None)


# --- the process end to end -------------------------------------------------------------------------------------------
def test_the_process_works_the_picked_listings_then_publishes_and_writes_its_summary(monkeypatch):
    s3 = _S3({f"{ROOT}chosen/done.json": _record("done", A), f"{RUN}{A}.main.jpg": b"m", f"{RUN}{A}.slab.png": _slab_png()})
    monkeypatch.setattr(proc, "sheet_listings", lambda client: [_listing("done", A), _listing("new", D)])
    cleared, worked = [], []
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: cleared.append(run_id) or [])

    def run(batch, sel, spend, run_id, deadline, say=print):
        worked.extend(l["key"] for l in sel.listings)
        return ["sentas d: no result after the GPU round"], {"chosen": [], "tracked": [], "pending": ["new"]}
    monkeypatch.setattr(proc, "run", run)
    import time
    summary = proc.process(s3, None, select.Spend(5.0, others=lambda: 1.0), "R1", time.monotonic() + 3600, say=lambda m: None)
    assert cleared == ["R1"] and worked == ["new"]                       # an earlier run's jobs are stopped first
    assert summary["listings"] == 1 and summary["pending"] == 1 and summary["problems"] == 1
    assert summary["published"]["changed"] and summary["month_spent_usd"] == 1.0
    stored = json.loads(s3.objects[f"{ROOT}summary.json"]["Body"])
    assert stored["pending_listings"] == ["new"] and stored["problem_list"] == ["sentas d: no result after the GPU round"]


def test_no_hosted_call_starts_after_the_work_window(monkeypatch):
    import time
    s3 = _S3({f"{ROOT}chosen/done.json": _record("done", A), f"{RUN}{A}.main.jpg": b"m"})
    monkeypatch.setattr(proc, "sheet_listings", lambda client: [_listing("new", D)])
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda batch, run_id: [])
    seen = []

    def run(batch, sel, spend, run_id, deadline, say=print):
        time.sleep(0.2)                                                     # the window (0.05 s) ends while working
        with pytest.raises(select.Halted, match="work window"):
            spend.reserve(0.01)
        seen.append(1)
        return [], {"chosen": [], "tracked": [], "pending": []}
    monkeypatch.setattr(proc, "run", run)
    proc.process(s3, None, select.Spend(5.0), "R1", time.monotonic() + 0.05, say=lambda m: None)
    assert seen == [1]


def test_a_close_up_choice_moves_to_a_whole_slab_once_the_judge_passes_one_and_a_rerun_costs_nothing(monkeypatch):
    """The pick, the GPU list, the judge, the hosted edit and the report over stored results: a listing whose chosen photo
    (A) is a close-up gets its whole slab (B) judged, the choice moves to it, and nothing is paid twice."""
    scraped = f"{ENV_SEGMENT}/products/scraped/sentas/"
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{RUN}{A}.main.jpg": b"ma", f"{RUN}judged/{A}.json":
              json.dumps({"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}).encode(),
              f"{RUN}{B}.main.jpg": b"mb", f"{RUN}{B}.slab.png": b"p", f"{RUN}held/{D}.json": b'{"reason": "x"}',
              f"{scraped}{B}.jpg": b"ob"})
    asked = []
    monkeypatch.setattr(select.judge, "judge", lambda o, r, f: asked.append(r) or (
        {"verdict": "pass", "problems": [], "version": C.JUDGE_VERSION}, 0.02))
    monkeypatch.setattr(select.clean, "submit", lambda *a: pytest.fail("a listing with a good photo was cleaned"))

    def picked():
        records = proc.choices(s3, ROOT)
        return proc.todo([_listing("one", A, B, D)], records, set(), proc.whole_slabs(s3, records))
    found = picked()
    assert [l["kept"] for l in found] == [(C.RUN_TAG, A)]
    sel = select.Selection(s3, found, C.RUN_TAG, C.CLEAN_TAG)
    assert sel.gpu_todo() == {}                                     # every candidate has its result: no GPU
    spend = select.Spend(5.0)
    select.judge_all(sel, spend, float("inf"))
    select.clean_all(sel, spend, float("inf"))
    assert asked == [b"mb"] and spend.spent == pytest.approx(0.02)
    result = select.report(sel)
    assert [c["sha"] for c in result["chosen"]] == [B]
    assert json.loads(s3.objects[f"{ROOT}chosen/one.json"]["Body"])["sha"] == B
    assert picked() == []                                           # a whole slab now: never picked again


def test_a_close_up_choice_with_no_whole_slab_left_to_try_costs_nothing_again(monkeypatch):
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{RUN}{A}.main.jpg": b"ma",
              f"{RUN}{B}.main.jpg": b"mb", f"{RUN}{B}.slab.png": b"p",
              f"{RUN}judged/{B}.json": json.dumps({"verdict": "fail", "problems": [{"kind": "light_reflection"}],
                                                   "version": C.JUDGE_VERSION}).encode()})
    monkeypatch.setattr(select.judge, "judge", lambda *a: pytest.fail("paid again"))
    monkeypatch.setattr(select.clean, "submit", lambda *a: pytest.fail("a listing with a good photo was cleaned"))
    records = proc.choices(s3, ROOT)
    sel = select.Selection(s3, proc.todo([_listing("one", A, B)], records, set(), proc.whole_slabs(s3, records)),
                           C.RUN_TAG, C.CLEAN_TAG)
    spend = select.Spend(5.0)
    assert sel.gpu_todo() == {}
    assert select.judge_all(sel, spend, float("inf")) == ([], False)
    assert select.clean_all(sel, spend, float("inf")) == ([], False)
    s3.written.clear()
    assert [c["sha"] for c in select.report(sel)["chosen"]] == [A]
    assert spend.spent == 0 and s3.written == [f"{ROOT}tracked.csv"]   # the kept record is not written again


class _Clients:
    def __init__(self, s3):
        self.s3 = s3

    def __call__(self, name, **kw):
        return self.s3 if name == "s3" else object()


def _main(monkeypatch, tmp_path=None, changed=True, capped=False, problems=0, relink_rc=0, writethrough_on=True):
    """main() with the process stubbed: what it returns and the relinks it made."""
    monkeypatch.setattr(proc, "unconfigured", lambda: [])
    monkeypatch.setattr(proc, "stop_on_signal", lambda spend: None)
    monkeypatch.setattr(proc.boto3, "client", _Clients(_S3()))
    monkeypatch.setattr(C, "BUDGET_USD", 10.0)
    monkeypatch.setattr(writethrough, "enabled", lambda: writethrough_on)
    if tmp_path is not None:
        monkeypatch.setenv("SCRAPER_RUN_SUMMARY", str(tmp_path / "summary.json"))
    else:
        monkeypatch.delenv("SCRAPER_RUN_SUMMARY", raising=False)
    relinks = []
    monkeypatch.setattr(proc, "relink", lambda: relinks.append(1) or relink_rc)

    def process(s3, batch, spend, run_id, deadline, say=print):
        spend.capped = capped
        return {"problems": problems, "pending": 0, "budget_reached": capped, "published": {"changed": changed}}
    monkeypatch.setattr(proc, "process", process)
    return proc.main(), relinks


def test_the_new_photos_are_linked_in_the_same_run_when_the_folder_changed(monkeypatch):
    assert _main(monkeypatch) == (0, [1])


def test_no_relink_when_nothing_changed(monkeypatch):
    assert _main(monkeypatch, changed=False) == (0, [])


def test_no_relink_where_there_is_no_ledger_to_link_into(monkeypatch):
    assert _main(monkeypatch, writethrough_on=False) == (0, [])


def test_a_run_at_the_budget_or_with_problems_still_succeeds_its_summary_says_so(monkeypatch, tmp_path):
    assert _main(monkeypatch, tmp_path, capped=True, problems=3) == (0, [1])
    handed = json.loads((tmp_path / "summary.json").read_text())
    assert handed["budget_reached"] and handed["problems"] == 3 and handed["relinked"]


def test_a_failed_relink_fails_the_run(monkeypatch):
    assert _main(monkeypatch, relink_rc=2) == (2, [1])


def test_the_process_is_named_by_the_runner_s_run_id_in_its_lock_and_its_jobs(monkeypatch):
    seen = {}
    monkeypatch.setenv("SCRAPER_RUN_ID", "20261102120000123")
    real = proc.exclusive
    monkeypatch.setattr(proc, "exclusive", lambda s3, owner, on_lost: seen.update(owner=owner) or real(s3, owner, on_lost))
    _main(monkeypatch)
    monkeypatch.setattr(proc, "process", lambda s3, batch, spend, run_id, deadline, say=print: seen.update(run_id=run_id) or
                        {"problems": 0, "pending": 0, "budget_reached": False, "published": {"changed": False}})
    proc.main()
    assert seen["owner"]["run_id"] == "20261102120000123" and seen["run_id"] == "20261102120000123"


def test_a_second_process_refuses_to_start_while_one_runs(monkeypatch):
    monkeypatch.setattr(proc, "exclusive", lambda s3, owner, on_lost: (_ for _ in ()).throw(proc.RunActive("held")))
    assert _main(monkeypatch)[0] == 4


def _reaping(monkeypatch, lock_body):
    s3 = _S3({proc.LOCK: lock_body})
    monkeypatch.setattr(proc.boto3, "client", _Clients(s3))
    done = []
    monkeypatch.setattr(proc.gpu, "stop_run", lambda batch, run_id: done.append(("stopped", run_id)) or ["job1"])
    monkeypatch.setattr(proc.gpu, "set_idle", lambda batch: done.append("idle"))
    monkeypatch.setattr(proc.gpu, "clear_leftovers", lambda *a: pytest.fail("touched another run's jobs"))
    return s3, done


def test_reaping_a_killed_run_stops_its_jobs_and_removes_its_own_lock(monkeypatch):
    s3, done = _reaping(monkeypatch, b'{"run_id": "R7", "tags": ["v2", "v2-clean"]}')
    proc.reap("R7")
    assert done == [("stopped", "R7"), "idle"] and proc.LOCK not in s3.objects


def test_reaping_never_touches_another_process_s_lock_or_ceiling(monkeypatch):
    # a rolling deploy's other task, or a process started by hand, holds the lock and the raised ceiling
    s3, done = _reaping(monkeypatch, b'{"run_id": "R8"}')
    proc.reap("R7")
    assert done == [("stopped", "R7")] and proc.LOCK in s3.objects                 # only the killed run's own jobs


def test_the_relink_is_the_produce_s_republish_in_this_process_with_its_progress_noise_dropped(monkeypatch, capsys):
    from stone_pipeline import produce
    seen = []

    def republish(argv):
        seen.append(argv)
        print("image 1 of 4,313")
        return 0
    monkeypatch.setattr(produce, "main", republish)
    assert proc.relink() == 0
    assert seen == [["--stage", "republish"]] and "image 1" not in capsys.readouterr().out


# --- the trigger: the runner's photos stage ---------------------------------------------------------------------------
def test_the_photos_run_follows_only_a_full_produce_never_the_publish_button_s_republish(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings.STANDARDISE, "AUTO", True)   # the class the runner reads at call time (a test reloads settings)
    started = []
    monkeypatch.setattr(runner, "start_run", lambda sources=None, stage="all", launch=None: started.append(stage) or ({}, 202))
    for stage, status in (("all", "succeeded"), ("republish", "succeeded"), ("catalog", "succeeded"), ("all", "failed"),
                          ("scrape", "succeeded"), ("inventory", "succeeded"), ("photos", "succeeded")):
        runner._follow_up({"run_id": "r", "stage": stage, "status": status})
    assert started == ["photos"]


def test_no_photos_run_follows_while_the_process_is_off(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings.STANDARDISE, "AUTO", False)
    monkeypatch.setattr(runner, "start_run", lambda **kw: pytest.fail("photos started while off"))
    runner._follow_up({"run_id": "r", "stage": "all", "status": "succeeded"})


def test_the_photos_stage_is_one_run_over_the_whole_environment(monkeypatch):
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    launched = []
    rec, code = runner.start_run(None, "photos", launch=lambda r: launched.append(r) or r.update(status="succeeded"))
    assert code == 202 and rec["stage"] == "photos" and rec["sources"] == [] and launched
    assert runner._build_command(launched[0]) == [sys.executable, "-u", "-m", "deploy.standardise_run"]
    rec, code = runner.start_run(["sentas"], "photos", launch=lambda r: pytest.fail("launched"))
    assert code == 400


class _Proc:
    stderr = None

    def __init__(self, lines, rc=0):
        self.stdout, self.rc = iter(lines), rc

    def wait(self):
        return self.rc

    def kill(self):
        pass


def _watch_quietly(monkeypatch):
    calls = []
    for name in ("_stamp_last_run", "_persist_run", "_follow_up"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_capture_counts", lambda: pytest.fail("a photos run reports its own counts"))
    for name in ("_persist_diagnostics", "_evaluate_admission"):
        monkeypatch.setattr(runner, name, lambda n=name: calls.append(n))
    return calls


def test_a_photos_run_s_record_carries_its_summary_and_its_report_is_streamed(monkeypatch, capsys, tmp_path):
    calls = _watch_quietly(monkeypatch)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r", "stage": "photos"}
    summary = {"chosen": 3, "review": 1, "pending": 2, "run_spent_usd": 1.5, "budget_reached": True}
    runner._summary_path(rec).write_text(json.dumps(summary))
    runner._watch_local(rec, _Proc(["==> 3 of 6 listings with a good photo\n"]))
    assert rec["status"] == "succeeded" and rec["counts"] == summary and calls == []
    assert not runner._summary_path(rec).exists()
    assert "3 of 6 listings" in capsys.readouterr().err


def test_a_photos_run_that_ended_without_a_summary_has_no_counts(monkeypatch, tmp_path):
    _watch_quietly(monkeypatch)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r", "stage": "photos"}
    runner._watch_local(rec, _Proc(["Traceback\n"], rc=1))
    assert rec["status"] == "failed" and rec["counts"] is None


def test_the_photo_process_s_stdout_is_its_report_and_its_summary_comes_back_in_a_file(monkeypatch, tmp_path):
    seen = {}

    def _popen(cmd, env=None, **kw):
        seen.update(kw, env=env)
        return type("P", (), {"poll": lambda self: None})()
    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(runner.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_child", None)          # no watcher runs here to clear the launched child
    monkeypatch.setattr(runner, "_watcher", None)
    rec = {"run_id": "r", "stage": "photos", "status": "queued"}
    runner._launch_local(rec)
    assert seen["stdout"] == subprocess.PIPE and seen["stderr"] == subprocess.STDOUT
    assert seen["env"]["SCRAPER_RUN_SUMMARY"] == str(runner._summary_path(rec))
    runner._launch_local({"run_id": "r2", "stage": "all", "status": "queued"})
    assert seen["stdout"] == subprocess.DEVNULL and seen["stderr"] == subprocess.PIPE
    assert "SCRAPER_RUN_SUMMARY" not in seen["env"]


# --- a clean stop -----------------------------------------------------------------------------------------------------
class _Child:
    def __init__(self, ends):
        self.ends, self.signals, self.waited = ends, [], None

    def poll(self):
        return None

    def terminate(self):
        self.signals.append("TERM")

    def wait(self, timeout=None):
        self.waited = timeout
        if not self.ends:
            raise subprocess.TimeoutExpired("x", timeout)
        return 0


def _child_of(monkeypatch, child, stage="photos"):
    run = {"run_id": "r", "stage": stage}
    monkeypatch.setattr(runner, "_child", child)
    monkeypatch.setattr(runner, "_child_run", run)
    monkeypatch.setattr(runner, "_watcher", None)
    return run


def test_a_stopping_server_passes_sigterm_to_the_running_child_and_waits_for_it(monkeypatch):
    child = _Child(ends=True)
    run = _child_of(monkeypatch, child)
    monkeypatch.setattr(runner, "_stopping", False)
    monkeypatch.setattr(runner, "RUN_STOP_GRACE_SECONDS", 5)
    runner.stop_current(clock=lambda: 0)
    assert child.signals == ["TERM"] and child.waited == 5 and run["signalled"]


def test_a_stopping_server_kills_a_late_child_but_leaves_its_clearing_to_the_next_run(monkeypatch):
    # the server's last save comes first: the next photos run's clear_leftovers and stale-lock takeover clear it
    child = _Child(ends=False)
    child.kill = lambda: child.signals.append("KILL")
    _child_of(monkeypatch, child)
    monkeypatch.setattr(runner, "_stopping", False)
    monkeypatch.setattr(runner, "_reap_photos", lambda *a: pytest.fail("cleared during the server's stop"))
    runner.stop_current()                                                  # logged, never raised
    assert child.signals == ["TERM", "KILL"]


def test_a_photos_run_killed_while_giving_way_has_what_it_left_cleared_by_its_run_id(monkeypatch):
    child = _Child(ends=False)
    child.kill = lambda: child.signals.append("KILL")
    run = _child_of(monkeypatch, child)
    reaped = []
    monkeypatch.setattr(runner, "_reap_photos", lambda run_id, deadline, clock: reaped.append((run_id, deadline)))
    runner._end_child(100.0, clock=lambda: 0, reap=True)
    assert child.signals == ["TERM", "KILL"] and reaped == [(run["run_id"], 100.0)]


def test_a_killed_produce_leaves_nothing_of_the_photo_process_to_clear(monkeypatch):
    child = _Child(ends=False)
    child.kill = lambda: child.signals.append("KILL")
    _child_of(monkeypatch, child, stage="all")
    monkeypatch.setattr(runner, "_reap_photos", lambda *a: pytest.fail("cleared a photo run that was not running"))
    runner._end_child(100.0, clock=lambda: 0, reap=True)


def test_clearing_a_killed_photos_run_never_outlasts_the_deadline(monkeypatch):
    import threading
    import time
    from deploy import standardise_run
    release, seen = threading.Event(), []
    monkeypatch.setattr(standardise_run, "reap", lambda run_id: seen.append(run_id) or release.wait(5))
    started = time.monotonic()
    runner._reap_photos("R7", started + 0.1, time.monotonic)
    assert time.monotonic() - started < 1 and seen == ["R7"]                # it goes on alone; the launch is not held
    release.set()


def test_one_deadline_bounds_the_child_s_grace_and_the_watcher_s_join(monkeypatch):
    child = _Child(ends=True)
    _child_of(monkeypatch, child)
    monkeypatch.setattr(runner, "_stopping", False)
    monkeypatch.setattr(runner, "RUN_STOP_GRACE_SECONDS", 80)
    joined = []
    monkeypatch.setattr(runner, "_watcher", type("W", (), {"join": lambda self, timeout: joined.append(timeout)})())
    times = iter([0, 0, 30])                                                # the child took 30 s to end
    runner.stop_current(clock=lambda: next(times))
    assert child.waited == 80 and joined == [50]


def test_a_run_marked_to_give_way_before_it_was_launched_never_starts(monkeypatch):
    # the gap: the slot is claimed for photos (queued), the request to give way comes before its launch
    for name in ("_stamp_last_run", "_persist_run"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_resolve_sources", lambda requested: sorted(requested or ["polonine"]))
    monkeypatch.setattr(runner, "_child", None)
    monkeypatch.setattr(runner, "_watcher", None)
    monkeypatch.setattr(runner, "_stopping", False)
    pending, launched = [], []
    photos, code = runner.start_run(None, "photos", launch=pending.append)          # claimed, launch still to come
    publish, code = runner.start_run(None, "republish", launch=lambda r: launched.append(r["run_id"]))
    try:
        assert publish["run_id"] != photos["run_id"]                                # claimed in the same millisecond
        assert code == 202 and runner._runs[photos["run_id"]]["preempted_by"]      # marked with the claim
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a run that gave way was launched"))
        runner._launch_local(pending[0])                                            # its launch comes late
        import time
        for _ in range(100):
            if launched:
                break
            time.sleep(0.02)
        assert launched == [publish["run_id"]]
        assert runner.get_run(photos["run_id"])["status"] == "stopped"
    finally:
        runner._runs[publish["run_id"]]["status"] = "succeeded"                      # free the slot for later tests


def test_no_run_is_launched_once_the_server_is_stopping(monkeypatch):
    monkeypatch.setattr(runner, "_stopping", True)
    monkeypatch.setattr(runner, "_persist_run", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("launched while the server stops"))
    rec = {"run_id": "r", "stage": "republish", "status": "queued"}
    runner._launch_local(rec)
    assert rec["status"] == "failed" and "stopping" in rec["error"]


def test_a_photos_run_that_ended_on_its_own_keeps_its_status_though_marked_to_give_way(monkeypatch, tmp_path):
    _watch_quietly(monkeypatch)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    rec = {"run_id": "r", "stage": "photos", "preempted_by": "republish run x"}     # marked, never signalled
    runner._watch_local(rec, _Proc(["==> done\n"], rc=0))
    assert rec["status"] == "succeeded"


def test_a_stopping_server_starts_no_follow_up_run(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings.STANDARDISE, "AUTO", True)
    monkeypatch.setattr(runner, "_stopping", True)
    monkeypatch.setattr(runner, "start_run", lambda **kw: pytest.fail("a run started while the server stops"))
    runner._follow_up({"run_id": "r", "stage": "all", "status": "succeeded"})


class _Running:
    """A photos process that runs until it gets SIGTERM, then cleans up and exits."""

    def __init__(self):
        import threading
        self.stopped, self.signals, self.stderr = threading.Event(), [], None

    @property
    def stdout(self):
        yield "==> standardise process R1\n"
        self.stopped.wait(5)

    def poll(self):
        return -15 if self.stopped.is_set() else None

    def terminate(self):
        self.signals.append("TERM")
        self.stopped.set()

    def wait(self, timeout=None):
        self.stopped.wait(timeout if timeout is not None else 5)
        return -15

    def kill(self):
        self.signals.append("KILL")
        self.stopped.set()


def test_the_owner_s_publish_stops_a_photos_run_cleanly_and_runs_at_once(monkeypatch, tmp_path):
    import time
    for name in ("_stamp_last_run", "_persist_run", "_persist_diagnostics", "_evaluate_admission"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_capture_counts", lambda: None)
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(runner, "_resolve_sources", lambda requested: sorted(requested or ["polonine"]))
    monkeypatch.setattr(runner, "_child", None)
    monkeypatch.setattr(runner, "_watcher", None)
    child = _Running()
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: child)
    photos, code = runner.start_run(None, "photos")
    assert code == 202
    for _ in range(100):                                                            # the photos run is under way
        if runner.get_run(photos["run_id"])["status"] == "running":
            break
        time.sleep(0.01)
    launched = []
    publish, code = runner.start_run(None, "republish", launch=lambda r: launched.append(r["run_id"]))
    assert code == 202 and publish["stage"] == "republish"
    try:
        for _ in range(100):
            if launched:
                break
            time.sleep(0.05)
        assert child.signals == ["TERM"] and launched == [publish["run_id"]]       # the photos run ended first
        record = runner.get_run(photos["run_id"])
        assert record["status"] == "stopped" and "republish" in record["error"]
    finally:
        runner._runs[publish["run_id"]]["status"] = "succeeded"                     # free the slot for later tests


def test_a_second_photos_run_or_a_produce_over_a_produce_is_still_refused(monkeypatch):
    monkeypatch.setattr(runner, "_stamp_last_run", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_resolve_sources", lambda requested: sorted(requested or ["polonine"]))
    held = []
    rec, code = runner.start_run(None, "photos", launch=lambda r: held.append(r))
    try:
        assert runner.start_run(None, "photos", launch=lambda r: pytest.fail("ran twice"))[1] == 409
    finally:
        held[0]["status"] = "succeeded"
    rec, code = runner.start_run(None, "republish", launch=lambda r: held.append(r))
    try:
        assert runner.start_run(None, "all", launch=lambda r: pytest.fail("ran twice"))[1] == 409
    finally:
        held[1]["status"] = "succeeded"


def test_the_server_s_sigterm_stops_the_run_before_its_last_config_save(monkeypatch):
    from stone_pipeline.config import server
    from stone_pipeline.ledger import snapshot
    order = []
    monkeypatch.setattr(runner, "stop_current", lambda: order.append("stop run"))
    monkeypatch.setattr(snapshot, "save_config", lambda db: order.append("save"))
    with pytest.raises(SystemExit):
        server.shutdown("config.db", type("P", (), {"stop": lambda self: order.append("periodic")})())
    assert order == ["stop run", "periodic", "save"]
