"""The photos run's scoped re-run list (PROD_3D_RUN_PLAN prerequisites 2 and 3): a scope file names the visible
listings to work, in order, and the gate misfits' photos to re-standardise once. With it, only the listed listings run;
each listed finished photo goes to the GPU once, its redraw a candidate the judge settles (never over the published
result), the note of the re-standardise rule its record; a refused redraw is listed with the judge's reason; an unknown
SKU or photo is refused at load; the run's cap is required and binds both budgets. Without it, the run is today's. No
network: S3 and the judge are stand-ins."""
from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ClientError

from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.photos import process, select, source
from stone_pipeline.tests.test_standardise_select import GPU, _listing, _round

A, B, D = "a" * 64, "b" * 64, "d" * 64


class _Store:
    """A bucket in a dict: get, put, copy, delete, list."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def copy_object(self, Bucket, Key, CopySource, ContentType, MetadataDirective):
        self.objects[Key] = self.objects[CopySource["Key"]]

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {"KeyCount": sum(k.startswith(Prefix) for k in self.objects)}

    def get_paginator(self, name):
        objects = self.objects

        class _Pages:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in sorted(objects) if k.startswith(Prefix)]}]
        return _Pages()


SCOPE_KEY = "prod/standardise-test/scopes/run-1.json"


def _scope_file(skus, restandardise=()):
    return _Store({SCOPE_KEY: json.dumps({"listings": list(skus), "restandardise": list(restandardise)}).encode()})


def _sku_listing(sku, *photos):
    return {**_listing(*photos, key=sku.lower()), "sku": sku}


LISTINGS = [_sku_listing("SEN-1", A), _sku_listing("SEN-2", B), _sku_listing("SEN-3", D)]


# --- the scope: only the listed listings, in the list's order ------------------------------------------------------

def test_with_a_scope_only_the_listed_listings_run_in_the_scope_s_order():
    scope = process.read_scope(_scope_file(["SEN-3", "SEN-1"]), SCOPE_KEY)
    assert [l["sku"] for l in process.scoped(LISTINGS, scope)] == ["SEN-3", "SEN-1"]


def test_without_a_scope_every_listing_runs_as_today():
    assert process.scoped(LISTINGS, None) == LISTINGS


def test_an_unknown_sku_in_the_scope_is_refused_naming_it():
    # a typo must never shrink the run silently
    scope = process.read_scope(_scope_file(["SEN-1", "SEN-9"]), SCOPE_KEY)
    with pytest.raises(process.ScopeRefused, match="SEN-9"):
        process.scoped(LISTINGS, scope)


SLAB_AND_BLOCK = [_sku_listing("SEN-1", A), {**_listing(B, key="sen-2", fmt="block"), "sku": "SEN-2"}]


def test_a_scope_naming_a_listing_that_gets_no_cut_out_is_refused_naming_it_and_its_format():
    # a block in a 3D scope is paid for and gets no cut-out: blocks get no 3D (the owner's rule)
    scope = process.read_scope(_scope_file(["SEN-1", "SEN-2"]), SCOPE_KEY)
    with pytest.raises(process.ScopeRefused, match=r"no 3D cut-out: SEN-2 \(block\)$"):
        process.scoped(SLAB_AND_BLOCK, scope)


def test_a_slab_only_scope_passes_unchanged_beside_a_block_on_the_sheet():
    scope = process.read_scope(_scope_file(["SEN-1"]), SCOPE_KEY)
    assert process.scoped(SLAB_AND_BLOCK, scope) == [SLAB_AND_BLOCK[0]]


def test_a_scope_file_that_is_no_scope_fails_loud_naming_its_key():
    with pytest.raises(process.ScopeRefused, match=SCOPE_KEY):
        process.read_scope(_Store({SCOPE_KEY: b'["SEN-1"]'}), SCOPE_KEY)


def test_a_restandardise_photo_must_be_a_finished_photo_of_a_listed_listing():
    rounds = [(C.RUN_TAG, {"sentas": _round(GPU, finished={A, B})}), (C.CLEAN_TAG, {"sentas": _round(GPU)})]
    listed = [_sku_listing("SEN-1", A), _sku_listing("SEN-3", D)]
    assert process.restandardise_items(process.read_scope(_scope_file(["SEN-1"], [A]), SCOPE_KEY), listed, rounds) == {
        ("sentas", A)}
    for sha, why in ((B, "no listed listing"), (D, "not finished")):     # B: SEN-2 is not listed; D: never finished
        scope = process.read_scope(_scope_file(["SEN-1", "SEN-3"], [sha]), SCOPE_KEY)
        with pytest.raises(process.ScopeRefused, match=sha[:12]):
            process.restandardise_items(scope, listed, rounds)


# --- re-standardise once ---------------------------------------------------------------------------------------------

def _selection(restandardise, restandardised=()):
    s = object.__new__(select.Selection)
    s.client, s.listings, s.exclude, s.owner, s.conflicts, s.chosen = _Store(), [], set(), {}, {}, []
    gpu = _round(GPU, finished={A}, slabs={A})
    gpu.restandardised = set(restandardised)
    s.sources, s.rounds = ["sentas"], [(C.RUN_TAG, {"sentas": gpu}), (C.CLEAN_TAG, {"sentas": _round(GPU)})]
    s.restandardise = frozenset(restandardise)
    return s


def test_a_listed_finished_photo_goes_to_the_gpu_once():
    assert _selection({("sentas", A)}).gpu_todo() == {"sentas": [A]}
    assert _selection({("sentas", A)}, restandardised={A}).gpu_todo() == {}        # its note: re-done, never again
    assert _selection(set()).gpu_todo() == {}                                      # no list: today's GPU list


def test_the_gpu_job_redoes_a_listed_photo_until_its_rule_is_noted():
    s3 = _Store({f"{GPU}{A}.main.jpg": b"old", f"{GPU}{A}.slab.png": b"old", source.checked_key(GPU, A): b"{}"})
    assert source.finished(s3, GPU, A)                                             # the outline rule alone: done
    assert source.restandardise_due(s3, GPU, A)
    s3.objects[source.checked_key(GPU, A, C.RESTANDARDISED)] = b'{"changed": true}'
    assert not source.restandardise_due(s3, GPU, A)


def _slab_result(changed):
    import numpy as np
    from stone_pipeline.standardise.photo import Result
    img = np.zeros((20, 30, 3), np.uint8)
    return Result(kind="slab", images={"main": img, "white": img}, slab=(img, np.ones((20, 30), np.float32)),
                  chosen=(0, 1), changed=changed)


def test_a_re_standardised_photo_is_a_candidate_for_the_judge_even_when_its_outline_reads_the_same():
    # the squaring fix never shows in layout.drawn_differently: the redraw is a candidate whatever it says, never
    # written over the published result
    s3 = _Store({f"{GPU}{A}.main.jpg": b"old", f"{GPU}{A}.slab.png": b"old"})
    assert source.rework(s3, GPU, A, _slab_result(changed=False), "restandardise") == "redrawn for the judge"
    assert json.loads(s3.objects[source.candidate_key(GPU, A)])["rule"] == C.RESTANDARDISED
    assert s3.objects[f"{GPU}{A}.main.jpg"] == b"old"


def _candidate(verdict):
    cand = source.candidate_prefix(GPU)
    files = [["slab.png", "image/png"], ["main.jpg", "image/jpeg"]]
    return _Store({f"{GPU}{A}.main.jpg": b"old", f"{GPU}{A}.slab.png": b"old", source.checked_key(GPU, A): b"{}",
                   f"{cand}{A}.main.jpg": b"new", f"{cand}{A}.slab.png": b"new",
                   f"{cand}judged/{A}.json": json.dumps({**verdict, "version": C.JUDGE_VERSION}).encode(),
                   source.candidate_key(GPU, A): json.dumps({"files": files, "rule": C.RESTANDARDISED}).encode()})


def test_a_re_standardised_candidate_the_judge_passes_is_promoted_and_noted():
    s3 = _candidate({"verdict": "pass", "problems": []})
    cands = select.Candidates(GPU, [k[len(GPU):] for k in s3.objects if k.startswith(GPU)])
    assert select.settle_one(s3, cands, "sentas", A)
    assert s3.objects[f"{GPU}{A}.main.jpg"] == b"new"
    assert json.loads(s3.objects[source.checked_key(GPU, A, C.RESTANDARDISED)]) == {"changed": True}


def test_a_re_standardised_candidate_the_judge_fails_keeps_the_result_and_is_listed_with_its_reason():
    s3 = _candidate({"verdict": "fail", "problems": [{"kind": "bad_fill", "where": "left edge"}]})
    cands = select.Candidates(GPU, [k[len(GPU):] for k in s3.objects if k.startswith(GPU)])
    assert select.settle_one(s3, cands, "sentas", A)
    assert s3.objects[f"{GPU}{A}.main.jpg"] == b"old"                            # the published result stands
    kept = process.restandardise_kept(s3, {("sentas", A)}, lambda src: GPU)
    assert kept == [{"source": "sentas", "sha": A, "why": "the judge failed its redraw (bad_fill): kept as it was"}]


# --- the run's cap ---------------------------------------------------------------------------------------------------

def test_a_scoped_run_without_its_cap_is_refused(monkeypatch):
    monkeypatch.setattr(C, "RUN_CAP_USD", None)
    assert any("SCRAPER_STANDARDISE_RUN_CAP_USD" in m for m in process.unconfigured(scoped=True))
    assert not any("RUN_CAP" in m for m in process.unconfigured(scoped=False))


def test_the_run_s_cap_binds_both_budgets_whatever_the_months_say():
    cap = select.RunCap(1.0)
    gpu = select.Spend(100.0, run=cap)
    clean = select.Spend(100.0, run=cap, cap_error=select.CleanCapReached)
    gpu.reserve(0.6)
    with pytest.raises(select.CleanCapReached, match="run"):
        clean.reserve(0.6)
    assert gpu.left() == pytest.approx(0.4) and clean.left() == pytest.approx(0.4)
    gpu.settle(0.6, 0.1)                                                           # settled to what it cost
    clean.reserve(0.6)


def test_a_refused_scope_does_nothing_at_all(monkeypatch):
    # review L1: the scope is checked before any housekeeping, so a refused one leaves the GPU leftovers and any half
    # settled recheck exactly as they were
    done = []
    monkeypatch.setattr(process.gpu, "clear_leftovers", lambda batch, run_id: done.append("clear_leftovers") or [])
    monkeypatch.setattr(process.select, "finish", lambda s3, tags: done.append("finish") or 0)
    monkeypatch.setattr(process, "sheet_and_photos", lambda s3: ("sheet", {}))
    monkeypatch.setattr(process, "photo_listings", lambda sheet, photos: LISTINGS)
    with pytest.raises(process.ScopeRefused, match="SEN-9"):
        process.process(_scope_file(["SEN-9"]), None, select.Spend(10.0), None, "R1", 0.0, lambda m: None,
                        scope_key=SCOPE_KEY)
    assert done == []
