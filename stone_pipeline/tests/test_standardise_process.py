"""The standardised-photo process (STANDARDISE_PROCESS.md), with AWS stand-ins: it picks exactly the listings without a
valid chosen photo, continues in the tags the published results came from, keeps one hosted budget per month across
runs, publishes only what changed, links the new photos in the same run only when the published folder changed, and runs
after a produce on the owner's button as the runner's photos stage."""
from __future__ import annotations

import hashlib
import io
import json
import math
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

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]["Body"])}

    def head_object(self, Bucket, Key):
        return {"ETag": self.objects[Key]["ETag"], "Metadata": self.objects[Key]["Metadata"]}

    def copy_object(self, Bucket, Key, CopySource, ContentType, MetadataDirective):
        self.written.append(Key)
        self.objects[Key] = dict(self.objects[CopySource["Key"]], Metadata={})

    def delete_object(self, Bucket, Key):
        self.written.append(Key)
        self.objects.pop(Key, None)

    def get_paginator(self, name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k, "ETag": v["ETag"]} for k, v in sorted(objects.items()) if k.startswith(Prefix)]}]
        return _Pages()


def _listing(key, *photos):
    return {"key": key, "source": "sentas", "format": "slab", "photos": list(photos)}


def _record(key, sha, prefix=RUN):
    return json.dumps({"source": "sentas", "key": key, "sha": sha, "tag": C.RUN_TAG, "prefix": prefix}).encode()


# --- what a run works on ----------------------------------------------------------------------------------------------
def test_the_process_picks_exactly_the_listings_without_a_valid_chosen_photo():
    listings = [_listing("done", A, B), _listing("new", D), _listing("moved", E), _listing("excluded", B)]
    records = {"done": {"sha": A}, "moved": {"sha": D}, "excluded": {"sha": B}, "gone": {"sha": E}}
    assert [l["key"] for l in proc.todo(listings, records, exclude={B})] == ["new", "moved", "excluded"]


def test_the_choice_records_are_read_from_the_clean_root():
    s3 = _S3({f"{ROOT}chosen/one.json": _record("one", A), f"{ROOT}tracked.csv": b"source\n", f"{ROOT}sentas/x.json": b"{}"})
    assert proc.choices(s3, ROOT) == {"one": json.loads(_record("one", A))}


def test_the_process_continues_in_the_tags_the_published_results_came_from():
    assert C.RUN_TAG == "v2" and C.CLEAN_TAGS == {"dev": "v2-clean", "prod": "v2-clean2"}
    assert C.CLEAN_TAG == C.CLEAN_TAGS[ENV_SEGMENT]


# --- one budget per month ---------------------------------------------------------------------------------------------
def test_the_month_s_budget_counts_every_run_of_the_month(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 1.0)
    s3 = _S3({proc.spend_key("2026-11"): json.dumps({"runs": {"20261101T000000Z": 0.9}}).encode()})
    spend = proc.month_spend(s3, "20261102T120000Z")
    with pytest.raises(select.SpendCapReached):
        spend.reserve(C.JUDGE_PRICE_USD)                                     # 0.90 + 0.14 would pass 1.00
    assert spend.capped


def test_a_new_month_has_its_whole_budget(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 1.0)
    s3 = _S3({proc.spend_key("2026-10"): json.dumps({"runs": {"20261031T230000Z": 1.0}}).encode()})
    proc.month_spend(s3, "20261101T000100Z").reserve(0.5)


def test_a_run_s_spend_is_kept_after_every_call_a_reserve_counting_until_it_is_settled(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 10.0)
    key, run_id = proc.spend_key("2026-11"), "20261102T120000Z"
    s3 = _S3({key: json.dumps({"runs": {"20261101T000000Z": 2.0}}).encode()})
    spend = proc.month_spend(s3, run_id)
    spend.reserve(0.14)
    runs = json.loads(s3.objects[key]["Body"])["runs"]
    assert runs["20261101T000000Z"] == 2.0 and runs[run_id] == pytest.approx(0.14)   # a run that dies now still counts it
    spend.settle(0.14, 0.02)
    assert json.loads(s3.objects[key]["Body"])["runs"][run_id] == pytest.approx(0.02)


def test_without_a_budget_the_process_refuses_to_start(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", None)
    for name, value in (("SCRAPER_GPU_QUEUE", "q"), ("SCRAPER_STANDARDISE_JOBDEF", "jd"), ("FAL_KEY", "k")):
        monkeypatch.setenv(name, value)
    assert proc.unconfigured() == ["SCRAPER_STANDARDISE_BUDGET_USD"]
    assert proc.main() == 2


def test_the_gpu_target_and_the_hosted_key_must_be_configured(monkeypatch):
    monkeypatch.setattr(C, "BUDGET_USD", 50.0)
    for name in ("SCRAPER_GPU_QUEUE", "BLOKPORT_GPU_QUEUE", "SCRAPER_STANDARDISE_JOBDEF", "BLOKPORT_STANDARDISE_JOBDEF", "FAL_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert proc.unconfigured() == ["SCRAPER_GPU_QUEUE", "SCRAPER_STANDARDISE_JOBDEF", "FAL_KEY"]


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


def test_publish_without_any_choice_record_is_refused():
    with pytest.raises(ValueError, match="no choice records"):
        pub.publish(_S3({f"{RUN}{A}.main.jpg": b"m"}), ROOT, say=lambda m: None)


# --- the process end to end -------------------------------------------------------------------------------------------
def test_the_process_works_the_picked_listings_then_publishes_and_writes_its_summary(monkeypatch):
    s3 = _S3({f"{ROOT}chosen/done.json": _record("done", A), f"{RUN}{A}.main.jpg": b"m"})
    monkeypatch.setattr(proc, "sheet_listings", lambda client: [_listing("done", A), _listing("new", D)])
    worked = []

    def run(batch, sel, spend, run_id, deadline, say=print):
        worked.extend(l["key"] for l in sel.listings)
        return ["sentas d: no result after the GPU round"], {"chosen": [], "tracked": [], "pending": ["new"]}
    monkeypatch.setattr(proc, "run", run)
    summary = proc.process(s3, None, select.Spend(5.0, before=1.0), "R1", math.inf, say=lambda m: None)
    assert worked == ["new"]
    assert summary["listings"] == 1 and summary["pending"] == ["new"] and summary["problems"] == 1
    assert summary["published"]["changed"] and summary["month_spent_usd"] == 1.0
    assert json.loads(s3.objects[f"{ROOT}summary.json"]["Body"]) == summary


class _Clients:
    def __init__(self, s3):
        self.s3 = s3

    def __call__(self, name, **kw):
        return self.s3 if name == "s3" else object()


def _main(monkeypatch, changed=True, capped=False, problems=0, relink_rc=0, writethrough_on=True):
    """main() with the process stubbed: what it returns and the relinks it made."""
    monkeypatch.setattr(proc, "unconfigured", lambda: [])
    monkeypatch.setattr(proc, "stop_on_signal", lambda spend: None)
    monkeypatch.setattr(proc.boto3, "client", _Clients(_S3()))
    monkeypatch.setattr(C, "BUDGET_USD", 10.0)
    monkeypatch.setattr(writethrough, "enabled", lambda: writethrough_on)
    relinks = []
    monkeypatch.setattr(proc, "relink", lambda: relinks.append(1) or relink_rc)

    def process(s3, batch, spend, run_id, deadline, say=print):
        spend.capped = capped
        return {"problems": problems, "published": {"changed": changed}}
    monkeypatch.setattr(proc, "process", process)
    return proc.main(), relinks


def test_the_new_photos_are_linked_in_the_same_run_when_the_folder_changed(monkeypatch):
    assert _main(monkeypatch) == (0, [1])


def test_no_relink_when_nothing_changed(monkeypatch):
    assert _main(monkeypatch, changed=False) == (0, [])


def test_no_relink_where_there_is_no_ledger_to_link_into(monkeypatch):
    assert _main(monkeypatch, writethrough_on=False) == (0, [])


def test_a_run_at_the_budget_still_relinks_what_it_published_and_says_so(monkeypatch):
    assert _main(monkeypatch, capped=True) == (3, [1])


def test_a_run_with_problems_is_run_again_and_a_failed_relink_fails_the_run(monkeypatch):
    assert _main(monkeypatch, problems=2) == (1, [1])
    assert _main(monkeypatch, relink_rc=2) == (2, [1])


def test_a_second_process_refuses_to_start_while_one_runs(monkeypatch):
    monkeypatch.setattr(proc, "exclusive", lambda s3, owner: (_ for _ in ()).throw(proc.RunActive("held")))
    assert _main(monkeypatch)[0] == 4


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
def test_the_photos_run_follows_each_produce_that_published_a_product_sheet(monkeypatch):
    from stone_pipeline.config import settings
    monkeypatch.setattr(settings.STANDARDISE, "AUTO", True)   # the class the runner reads at call time (a test reloads settings)
    started = []
    monkeypatch.setattr(runner, "start_run", lambda sources=None, stage="all", launch=None: started.append(stage) or ({}, 202))
    for stage, status in (("all", "succeeded"), ("republish", "succeeded"), ("catalog", "succeeded"), ("all", "failed"),
                          ("scrape", "succeeded"), ("inventory", "succeeded"), ("photos", "succeeded")):
        runner._follow_up({"run_id": "r", "stage": stage, "status": status})
    assert started == ["photos", "photos", "photos"]


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


def test_the_photo_process_report_is_streamed_and_it_is_no_validated_produce(monkeypatch, capsys):
    calls = []
    for name in ("_capture_counts", "_stamp_last_run", "_persist_run", "_follow_up"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    for name in ("_persist_diagnostics", "_evaluate_admission"):
        monkeypatch.setattr(runner, name, lambda n=name: calls.append(n))

    class _Proc:
        stderr = None
        stdout = iter(["==> 3 of 4 listings with a good photo\n"])

        def wait(self):
            return 0

        def kill(self):
            pass
    rec = {"run_id": "r", "stage": "photos"}
    runner._watch_local(rec, _Proc())
    assert rec["status"] == "succeeded" and calls == []
    assert "3 of 4 listings" in capsys.readouterr().err


def test_the_photo_process_s_stdout_is_its_report_and_is_kept(monkeypatch):
    seen = {}

    def _popen(cmd, env=None, **kw):
        seen.update(kw)
        return object()
    monkeypatch.setattr(subprocess, "Popen", _popen)
    monkeypatch.setattr(runner.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: None})())
    runner._launch_local({"run_id": "r", "stage": "photos", "status": "queued"})
    assert seen["stdout"] == subprocess.PIPE and seen["stderr"] == subprocess.STDOUT
    runner._launch_local({"run_id": "r", "stage": "all", "status": "queued"})
    assert seen["stdout"] == subprocess.DEVNULL and seen["stderr"] == subprocess.PIPE
