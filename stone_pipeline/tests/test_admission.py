"""Phase 2b: source admission. A source becomes ELIGIBLE for auto only after N consecutive CLEAN runs
(health OK + no drift) and (if required) certification; a drift on an `auto` source auto-DEMOTES it to
review. The decision core is a pure function; the reaction is wired through the config store.
"""

from __future__ import annotations

from stone_pipeline.config import admission, store
from stone_pipeline.config.admission import AUTO, DEMOTED, ELIGIBLE, REVIEW
from stone_pipeline.config.settings import Admission
from stone_pipeline.config.sources import SourceConfig, load_source

RULE = Admission(consistent_runs=2, require_certify=False, history_limit=10)


def _ev(health="OK", drift=None, worst="OK", note=None, certified=True):
    return {"health": health, "drift": drift or [], "worst": worst, "note": note, "certified": certified}


def _summary(run_id, health="OK", drift=None):
    return {"run_id": run_id, "health": health, "drift": drift or [], "stages": [], "gates": {}}


# --- the pure decision core ---------------------------------------------------
def test_streak_breaks_on_drift_or_fail():
    assert admission.streak([_ev(), _ev(), _ev()]) == 3
    assert admission.streak([_ev(), _ev(drift=["fill_drop"]), _ev()]) == 1   # drift breaks it
    assert admission.streak([_ev(health="FAILED")]) == 0
    assert admission.streak([_ev(worst="FAILED")]) == 0                       # a FAILED gate breaks it


def test_compute_states():
    clean = [_ev(), _ev()]
    assert admission.compute(clean, certified=True, mode="review", rule=RULE)["state"] == ELIGIBLE
    assert admission.compute([_ev()], certified=True, mode="review", rule=RULE)["state"] == REVIEW  # streak 1 < 2
    assert admission.compute(clean, certified=True, mode="auto", rule=RULE)["state"] == AUTO
    demoted = [_ev(health="DEGRADED", drift=["fill_drop"], note="demoted")]
    assert admission.compute(demoted, certified=True, mode="review", rule=RULE)["state"] == DEMOTED


def test_require_certify_blocks_eligibility():
    rule = Admission(consistent_runs=2, require_certify=True, history_limit=10)
    clean = [_ev(), _ev()]
    assert admission.compute(clean, certified=False, mode="review", rule=rule)["state"] == REVIEW
    assert admission.compute(clean, certified=True, mode="review", rule=rule)["state"] == ELIGIBLE


def test_nonsensical_threshold_fails_loud():
    import pytest
    with pytest.raises(ValueError, match="CONSISTENT_RUNS"):
        Admission(consistent_runs=0)            # would make any source eligible with zero clean runs
    with pytest.raises(ValueError):
        Admission(history_limit=0)


# --- the store history ---------------------------------------------------------
def test_run_event_round_trip_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    for i in range(5):
        store.record_run_event("polonine", f"polonine_{i:02d}", "OK", "OK", [], True, limit=3)
    evs = store.read_run_events("polonine")
    assert len(evs) == 3 and evs[0]["run_id"] == "polonine_04"     # newest first, bounded to 3
    # absent db never created by a read (isolation contract)
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))
    assert store.read_run_events("polonine") == [] and not (tmp_path / "absent.db").exists()


# --- record + react ------------------------------------------------------------
def test_drift_auto_demotes_an_auto_source(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_source(SourceConfig(source="polonine", adapter="polonine", source_code="pol", mode="auto"))
    res = admission.record_and_evaluate(
        "polonine", _summary("polonine_x", health="DEGRADED", drift=[{"kind": "fill_drop", "field": "color"}]),
        rule=RULE)
    assert res["demoted"] is True
    assert load_source("polonine").mode == "review"                # pulled back out of auto
    assert store.read_run_events("polonine")[0]["note"] == "demoted"


def test_empty_scrape_abort_demotes_but_a_skip_does_not(tmp_path, monkeypatch):
    # H1 contract: a floor-abort (empty scrape) stamps health="FAILED" into the run's diagnostics, so an
    # auto source MUST be demoted. The twin: a genuine load_frame SKIP writes no health ("" -> coerced OK)
    # and must NOT demote. This is why the abort has to write "FAILED" and not leave health empty.
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_source(SourceConfig(source="polonine", adapter="polonine", source_code="pol", mode="auto"))
    failed = admission.record_and_evaluate("polonine", _summary("pol_fail", health="FAILED"), rule=RULE)
    assert failed["demoted"] is True and load_source("polonine").mode == "review"
    store.upsert_source(SourceConfig(source="varsha", adapter="varsha", source_code="var", mode="auto"))
    skip = admission.record_and_evaluate("varsha", {"run_id": "var_skip", "stages": [], "gates": {}}, rule=RULE)
    assert skip["demoted"] is False and load_source("varsha").mode == "auto"


def test_clean_run_on_review_source_does_not_demote(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_source(SourceConfig(source="varsha", adapter="varsha", source_code="var", mode="review"))
    res = admission.record_and_evaluate("varsha", _summary("varsha_1"), rule=RULE)
    assert res["demoted"] is False and load_source("varsha").mode == "review"


def test_status_for_becomes_eligible_after_consistent_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_source(SourceConfig(source="polonine", adapter="polonine", source_code="pol", mode="review"))
    for i in range(2):
        admission.record_and_evaluate("polonine", _summary(f"polonine_{i}"), rule=RULE)
    st = admission.status_for("polonine", rule=RULE)
    assert st["state"] == ELIGIBLE and st["streak"] >= 2


# --- the diagnostics surface carries admission --------------------------------
def test_diagnostics_payload_includes_admission(tmp_path, monkeypatch):
    from stone_pipeline.config import diagnostics
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_source(SourceConfig(source="polonine", adapter="polonine", source_code="pol", mode="review"))
    store.record_source_diagnostic("polonine", "polonine_x", {"source": "polonine", "health": "OK", "stages": []})
    got = diagnostics.read_source("polonine")
    assert got["admission"]["state"] in (REVIEW, ELIGIBLE, AUTO, DEMOTED)
