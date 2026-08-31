"""Auto-enhance trigger: after a produce stages new raw images, submit the GPU reprocess for ONLY the new
ones (the delta), auto-sliced, with CLASSIFY=false. Off unless BLOKPORT_AUTO_ENHANCE; best-effort.

The delta is scraped - (enhanced markers + discarded markers) -- NOT scraped - improved, because produce
on the torch-free :core writes a raw re-encode into improved/ without enhancing. These tests pin that
detection and the submit parameters with boto3 fully mocked (no AWS).
"""

from __future__ import annotations

import types

import pytest

from stone_pipeline.config.settings import AutoEnhanceConfig
from deploy import enhance_trigger as et

A, B, C = "a" * 64, "b" * 64, "c" * 64


class _Paginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}


class FakeS3:
    def __init__(self, keys):
        self.keys = keys

    def get_paginator(self, _name):
        return _Paginator(self.keys)


class FakeBatch:
    def __init__(self):
        self.calls = []

    def submit_job(self, **kw):
        self.calls.append(kw)
        return {"jobId": f"job-{len(self.calls)}"}


def _keys(scraped=(), enhanced=(), discarded=(), src="varsha"):
    k = []
    k += [f"dev/products/scraped/{src}/{s}.jpg" for s in scraped]
    k += [f"dev/products/enhanced/{src}/{s}.txt" for s in enhanced]
    k += [f"dev/products/discarded/{src}/{s}.json" for s in discarded]
    return k


def test_done_and_pending_delta():
    # scraped {A,B,C}; A enhanced, B discarded -> only C is pending
    s3 = FakeS3(_keys(scraped=(A, B, C), enhanced=(A,), discarded=(B,)))
    assert et.done_shas(s3, "varsha") == {A, B}
    assert et.pending_shas(s3, "varsha") == {C}


def test_nothing_pending_when_all_done():
    s3 = FakeS3(_keys(scraped=(A, B), enhanced=(A,), discarded=(B,)))
    assert et.pending_shas(s3, "varsha") == set()


D = "d" * 64


def test_image_progress_mid_generation():
    # scraped {A,B,C,D}; A,B enhanced, C discarded -> D still generating
    s3 = FakeS3(_keys(scraped=(A, B, C, D), enhanced=(A, B), discarded=(C,)))
    assert et.image_progress(s3, "varsha") == {
        "total": 4, "ready": 2, "held": 1, "generating": True}


def test_image_progress_complete_flips_generating_false():
    # every scraped image has a marker (ready + held == total) -> done
    s3 = FakeS3(_keys(scraped=(A, B, C), enhanced=(A, B), discarded=(C,)))
    assert et.image_progress(s3, "varsha") == {
        "total": 3, "ready": 2, "held": 0 + 1, "generating": False}


def test_image_progress_none_when_no_scraped():
    # no scraped originals for this source -> omit the block (UI shows "-")
    s3 = FakeS3(_keys(enhanced=(A,)))
    assert et.image_progress(s3, "varsha") is None


def test_image_progress_ignores_stale_markers():
    # an enhanced marker whose original is gone from scraped/ must NOT inflate ready past total
    s3 = FakeS3(_keys(scraped=(A,), enhanced=(A, B)))
    assert et.image_progress(s3, "varsha") == {
        "total": 1, "ready": 1, "held": 0, "generating": False}


def test_image_progress_both_markers_count_ready_not_held():
    # A carries BOTH markers (a terminal hold that later succeeded on a FULL re-run): it counts as READY
    # only -- the enhanced/published marker wins -- so ready + held never exceeds total.
    s3 = FakeS3(_keys(scraped=(A, B), enhanced=(A, B), discarded=(A,)))
    assert et.image_progress(s3, "varsha") == {
        "total": 2, "ready": 2, "held": 0, "generating": False}


def _wire(monkeypatch, s3, batch, cfg, watermarked=("varsha",)):
    monkeypatch.setattr(et, "SETTINGS", types.SimpleNamespace(auto_enhance=cfg))
    monkeypatch.setattr(et, "_watermarked_sources", lambda: set(watermarked))
    monkeypatch.setattr("boto3.client", lambda service, region_name=None: s3 if service == "s3" else batch)


def test_disabled_is_noop(monkeypatch):
    batch = FakeBatch()
    _wire(monkeypatch, FakeS3(_keys(scraped=(A,))), batch,
          AutoEnhanceConfig(enabled=False, queue="q", job_definition="jd"))
    assert et.submit_pending(["varsha"]) == []
    assert batch.calls == []


def test_enabled_but_unconfigured_is_noop(monkeypatch):
    batch = FakeBatch()
    _wire(monkeypatch, FakeS3(_keys(scraped=(A,))), batch,
          AutoEnhanceConfig(enabled=True, queue="", job_definition=""))
    assert et.submit_pending(["varsha"]) == []
    assert batch.calls == []


def test_submits_only_delta_with_correct_params(monkeypatch):
    batch = FakeBatch()
    # scraped {A,B,C}; A already enhanced -> pending {B,C} = 2 images
    s3 = FakeS3(_keys(scraped=(A, B, C), enhanced=(A,)))
    _wire(monkeypatch, s3, batch,
          AutoEnhanceConfig(enabled=True, queue="Q", job_definition="JD", slice_size=200),
          watermarked=("varsha",))
    ids = et.submit_pending(["varsha"])
    assert len(ids) == 1 and len(batch.calls) == 1          # 2 images, slice 200 -> one job
    call = batch.calls[0]
    assert call["jobQueue"] == "Q" and call["jobDefinition"] == "JD"
    env = {e["name"]: e["value"] for e in call["containerOverrides"]["environment"]}
    assert env["SRC"] == "varsha"
    assert env["WATERMARKED"] == "true"                     # varsha is watermarked
    assert env["CLASSIFY"] == "false"                       # auto-enhance NEVER auto-discards
    assert env["SLICE_OFFSET"] == "0"


def test_auto_slices_large_delta(monkeypatch):
    batch = FakeBatch()
    shas = [f"{i:064x}" for i in range(5)]                  # 5 pending, all of the (sorted) scraped list
    s3 = FakeS3(_keys(scraped=shas))
    _wire(monkeypatch, s3, batch,
          AutoEnhanceConfig(enabled=True, queue="Q", job_definition="JD", slice_size=2),
          watermarked=())
    et.submit_pending(["varsha"])
    # 5 images across windows of 2 -> 3 windows at offsets 0,2,4 (into the stable full sorted list)
    offsets = sorted(
        int({e["name"]: e["value"] for e in c["containerOverrides"]["environment"]}["SLICE_OFFSET"])
        for c in batch.calls)
    assert offsets == [0, 2, 4]
    env0 = {e["name"]: e["value"] for e in batch.calls[0]["containerOverrides"]["environment"]}
    assert env0["WATERMARKED"] == "false"                  # enhance-only source


def test_slices_target_the_window_of_the_pending_image(monkeypatch):
    # REGRESSION for the slice-race bug: only the LAST image (sorted position 5) is pending; every slice must
    # index the STABLE full sorted list, so the job must target ITS window (offset 4), NOT offset 0. The old
    # ceil(pending/size)-from-0 logic submitted offset 0 and would have processed the wrong window -> the
    # pending image gets no marker -> held dark. Window-targeting fixes it.
    batch = FakeBatch()
    shas = [f"{i:064x}" for i in range(6)]                  # sorted positions 0..5
    s3 = FakeS3(_keys(scraped=shas, enhanced=shas[:5]))     # first 5 done, only position 5 pending
    _wire(monkeypatch, s3, batch,
          AutoEnhanceConfig(enabled=True, queue="Q", job_definition="JD", slice_size=2), watermarked=())
    et.submit_pending(["varsha"])
    offsets = [int({e["name"]: e["value"] for e in c["containerOverrides"]["environment"]}["SLICE_OFFSET"])
               for c in batch.calls]
    assert offsets == [4]                                  # window containing position 5, never 0


def test_no_job_when_nothing_pending(monkeypatch):
    batch = FakeBatch()
    s3 = FakeS3(_keys(scraped=(A,), enhanced=(A,)))         # the one image is already enhanced
    _wire(monkeypatch, s3, batch,
          AutoEnhanceConfig(enabled=True, queue="Q", job_definition="JD"))
    assert et.submit_pending(["varsha"]) == []
    assert batch.calls == []


def test_fan_out_is_capped_to_max_jobs(monkeypatch):
    # F13: each Batch job carries its own fal_max_usd ceiling, so the TOTAL job count per trigger is capped
    # to bound worst-case FAL spend. 10 pending -> 5 windows of 2, but max_jobs=3 -> only 3 jobs submitted;
    # the deferred windows stay un-done and ride the next trigger.
    batch = FakeBatch()
    shas = [f"{i:064x}" for i in range(10)]
    s3 = FakeS3(_keys(scraped=shas))
    _wire(monkeypatch, s3, batch,
          AutoEnhanceConfig(enabled=True, queue="Q", job_definition="JD", slice_size=2, max_jobs=3),
          watermarked=())
    ids = et.submit_pending(["varsha"])
    assert len(ids) == 3 and len(batch.calls) == 3          # capped at 3, not 5


def test_max_jobs_zero_disables_the_cap(monkeypatch):
    batch = FakeBatch()
    shas = [f"{i:064x}" for i in range(10)]                  # 5 windows
    s3 = FakeS3(_keys(scraped=shas))
    _wire(monkeypatch, s3, batch,
          AutoEnhanceConfig(enabled=True, queue="Q", job_definition="JD", slice_size=2, max_jobs=0),
          watermarked=())
    assert len(et.submit_pending(["varsha"])) == 5          # <=0 -> no cap, all windows submitted


def test_single_source_produce_triggers_auto_enhance(monkeypatch):
    # Regression: run_all() fired the GPU reprocess for the "all" produce, but a SINGLE-source produce
    # (build --sources X / the per-source loop) reaches run.main() on its own branch and must also trigger
    # it -- else a single-source cold start stages raw images that never get de-watermarked.
    from stone_pipeline import run as run_mod
    called = []
    monkeypatch.setattr(run_mod, "run_source", lambda target: types.SimpleNamespace(source=target))
    monkeypatch.setattr(run_mod, "print_summary", lambda manifest: None)
    monkeypatch.setattr(et, "submit_pending", lambda sources: called.append(list(sources)) or [])
    assert run_mod.main(["varsha"]) == 0
    assert called == [["varsha"]]                            # the single source was handed to the trigger
