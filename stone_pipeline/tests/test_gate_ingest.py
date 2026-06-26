"""Ingest gate: the post-scrape check. Reports (does not reject) what the scrape is
missing per the ingest contract, and escalates to FAILED when a field is absent
across the whole batch (a systemically incomplete scrape, caught right after the
scraper instead of at the Medusa upload screen).
"""

from __future__ import annotations

from stone_pipeline import gates
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.gates import definitions as gate_defs


def _row(**kw) -> CanonicalRow:
    base = dict(src_site="polonine", surrogate_key="620", raw_name="Verde Ubatuba",
                raw_thickness="2", raw_image_urls=["http://x/1.jpg"])
    base.update(kw)
    return CanonicalRow(**base)


def test_complete_row_passes_clean():
    report = gates.apply([_row()], gate_defs.INGEST)
    assert report.status == gates.OK
    assert not report.violations


def test_ingest_is_report_only_never_rejects():
    # a row missing every required raw field is reported, but the gate does NOT mutate it
    # (no reject, no flag) -- ingest is a visibility check, not a row filter.
    row = _row(raw_name="", variety_match_key="", raw_thickness="", raw_image_urls=[])
    report = gates.apply([row], gate_defs.INGEST)
    assert report.rows_rejected == 0
    assert report.rows_flagged == 0
    assert row.is_emittable  # untouched
    assert not row.review_flags


def test_systemic_missing_field_fails_the_batch():
    # whole batch lacks a variety name -> a broken scrape/adapter, not unlucky rows -> FAILED
    rows = [_row(raw_name="", variety_match_key="") for _ in range(10)]
    report = gates.apply(rows, gate_defs.INGEST)
    assert report.status == gates.FAILED
    assert report.violations["ingest_missing_variety_name"] == 10


def test_partial_missing_field_warns_not_fails():
    rows = [_row() for _ in range(8)] + [_row(raw_image_urls=[]) for _ in range(2)]
    report = gates.apply(rows, gate_defs.INGEST)
    # 20% missing images is below the warn fraction -> still OK, but counted
    assert report.status == gates.OK
    assert report.violations.get("ingest_missing_image_source") == 2
