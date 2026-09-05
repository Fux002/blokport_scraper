"""The separate per-vendor origin-confirmation queue: store round-trip, overlay into origin_overrides, the
produce-side queue population from held rows, and the reset wipe. Uses the autouse per-test config.db.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store as ds
from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.reference.loaders import OriginOverrides, _norm
from stone_pipeline.stages import decisions


def _ref(source, variety, stype):
    return f"{_norm(source)}|{_norm(variety)}|{_norm(stype)}"


# -- store round-trip ----------------------------------------------------------
def test_set_and_read_origin_decision_roundtrip():
    ds.set_origin_decision("marenostone", "Crystal White", "Granite", "ir")   # any casing
    assert ds.origin_decisions()[(_norm("marenostone"), _norm("Crystal White"), _norm("Granite"))] == "IR"


def test_origin_decision_is_scoped_per_vendor_and_per_type():
    ds.set_origin_decision("marenostone", "Crystal White", "Granite", "IR")
    ds.set_origin_decision("zucchi", "Crystal White", "Granite", "CN")
    ds.set_origin_decision("marenostone", "Crystal White", "Marble", "IT")
    d = ds.origin_decisions()
    assert d[(_norm("marenostone"), _norm("Crystal White"), _norm("Granite"))] == "IR"
    assert d[(_norm("zucchi"), _norm("Crystal White"), _norm("Granite"))] == "CN"
    assert d[(_norm("marenostone"), _norm("Crystal White"), _norm("Marble"))] == "IT"


def test_invalid_origin_decision_raises():
    with pytest.raises(ds.InvalidDecision):
        ds.set_origin_decision("marenostone", "Crystal White", "Granite", "")   # no country
    with pytest.raises(ds.InvalidDecision):
        ds.set_origin_decision("", "Crystal White", "Granite", "IR")            # no source


# -- overlay into the derive lookup --------------------------------------------
def test_overlay_feeds_origin_overrides_lookup():
    ds.set_origin_decision("marenostone", "Crystal White", "Granite", "IR")
    ov = OriginOverrides()
    ov.apply_overlay(ds.origin_decisions())
    assert ov.lookup("marenostone", "Crystal White", "Granite") == "IR"
    assert ov.lookup("zucchi", "Crystal White", "Granite") is None            # scoped to the vendor


def test_clear_origin_decisions_wipes_the_overlay():
    ds.set_origin_decision("marenostone", "Crystal White", "Granite", "IR")
    assert ds.origin_decisions()
    assert ds.clear_origin_decisions() == 1
    assert ds.origin_decisions() == {}


# -- produce-side queue population ---------------------------------------------
def _held(source="marenostone", variety="Crystal White", stype="Granite", map_c="IN", vendor="IR"):
    row = CanonicalRow(src_site=source, surrogate_key="1", variation_name=variety, type_name=stype)
    row.origin_source = "origin_needs_confirmation"
    row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_needs_confirmation,
                            raw_value=map_c, best_guess=vendor, confidence=Confidence.none, method="x"))
    return row


def test_write_origin_confirm_file_dedupes_and_carries_payload():
    rows = [_held(), _held(),                                                  # dup (source, variety, type)
            _held(variety="Golden Silver", stype="Travertine", map_c="TR")]
    assert decisions.write_origin_confirm_file(rows) == 2
    by_ref = {p["ref"]: p for p in ds.list_pending("origin")}
    cw = by_ref[_ref("marenostone", "Crystal White", "Granite")]
    assert cw["source"] == "marenostone" and cw["stone_type"] == "Granite"
    assert cw["map_country"] == "IN" and cw["vendor_origin"] == "IR"
    assert cw["current_country"] is None                                      # not yet decided


def test_write_origin_confirm_file_ignores_resolved_rows():
    good = CanonicalRow(src_site="marenostone", surrogate_key="2", variation_name="Abadeh", type_name="Marble")
    good.origin_source = "vendor_origin"                                       # resolved, not held
    assert decisions.write_origin_confirm_file([good]) == 0
    assert ds.list_pending("origin") == []


def test_decided_country_surfaces_in_queue_listing():
    decisions.write_origin_confirm_file([_held()])
    ds.set_origin_decision("marenostone", "Crystal White", "Granite", "IR")
    item = ds.list_pending("origin")[0]
    assert item["current_country"] == "IR"


def test_origin_queue_cleared_by_review_pending_clear():
    decisions.write_origin_confirm_file([_held()])
    assert ds.list_pending("origin")
    ds.clear_review_pending()                                                  # a reset path
    assert ds.list_pending("origin") == []


def test_write_curation_threads_rows_to_origin_queue(tmp_path, monkeypatch):
    """REGRESSION: write_curation must pass `rows` to write_origin_confirm_file. A prior version referenced
    an undefined `rows` inside write_curation (only `result` was in scope), which NameError-crashed the whole
    produce at the catalog step. This drives the REAL write_curation path with a held row."""
    import dataclasses
    from stone_pipeline.stages import curate, decisions
    from stone_pipeline.stages.curate import CurationResult, BRANCHES
    from stone_pipeline.config.settings import SETTINGS
    dirs = {}
    for attr in ("to_upload_dir", "catalog_source_dir", "review_dir"):
        d = tmp_path / attr
        d.mkdir(parents=True, exist_ok=True)
        dirs[attr] = d
    new = dataclasses.replace(SETTINGS, paths=dataclasses.replace(SETTINGS.paths, **dirs))
    monkeypatch.setattr(curate, "SETTINGS", new)                    # curate writes into tmp, not the repo
    monkeypatch.setattr(decisions, "save_rejected", lambda *a, **k: None)          # unrelated writers off
    monkeypatch.setattr(decisions, "write_backbone_leaf_pending", lambda *a, **k: 0)
    result = CurationResult(alias_additions={b: [] for b in BRANCHES},
                            new_variants={b: [] for b in BRANCHES},
                            backbone_new={b: [] for b in BRANCHES})
    curate.write_curation(result, [_held()])          # must NOT raise NameError
    assert [o["variety"] for o in ds.list_pending("origin")] == ["Crystal White"]
