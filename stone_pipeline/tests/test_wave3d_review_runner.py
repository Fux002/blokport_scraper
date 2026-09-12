"""Wave 3d: three silent outcomes in the control plane.

A produce whose deliverables never reached S3 reported "succeeded" (Blokport then pulls stale keys); a
malformed integer on the admin source PUT reached int() and answered 500; and two pending varieties with the
same name but different stone types were collapsed into one review card (a decision on the card would type
the second vendor's listing as the first's).
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store, runner, server


class _Proc:
    stderr = iter(())

    def wait(self):
        return 0

    def kill(self):
        pass


def _quiet_runner(monkeypatch):
    for name in ("_capture_counts", "_stamp_last_run", "_persist_run", "_persist_diagnostics", "_evaluate_admission"):
        monkeypatch.setattr(runner, name, lambda *a, **k: {} if name == "_capture_counts" else None)
    from stone_pipeline.ledger import snapshot
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: None)


def test_a_publish_failure_fails_the_run_instead_of_reporting_success(monkeypatch):
    _quiet_runner(monkeypatch)
    from deploy import upload_artifacts

    def _boom(run_id=None):
        raise RuntimeError("S3 unreachable")
    monkeypatch.setattr(upload_artifacts, "main", _boom)
    rec = {"run_id": "r1", "stage": "all", "status": "queued"}
    runner._watch_local(rec, _Proc())
    assert rec["status"] == "failed"
    assert "publish" in rec["error"] and "S3 unreachable" in rec["error"]


def test_a_successful_publish_keeps_the_run_succeeded(monkeypatch):
    _quiet_runner(monkeypatch)
    from deploy import upload_artifacts
    monkeypatch.setattr(upload_artifacts, "main", lambda run_id=None: None)
    rec = {"run_id": "r2", "stage": "all", "status": "queued"}
    runner._watch_local(rec, _Proc())
    assert rec["status"] == "succeeded"


@pytest.mark.parametrize("field", ["default_bundle_size", "min_expected_rows"])
def test_a_malformed_integer_on_the_source_put_is_a_400_naming_the_field(field):
    body = {"source_code": "POL", "ports": ["port_1"], field: "abc"}
    result = server._validate_source_put("polonine", body)
    assert result is not None
    status, err = result
    assert status == 400 and field in err["error"]


@pytest.mark.xfail(strict=True, reason="reproduced: variety cards are keyed by norm(name) only, so two types "
                   "collapse into one card; the ref shape is load-bearing for the reject PUT and Blokport's "
                   "review UI, so the fix is the review-keying item of the structure wave")
def test_two_pending_varieties_with_the_same_name_and_different_types_are_two_cards():
    from stone_pipeline.stages import decisions as stage_decisions
    pending = [{"variant": "Imperial White", "reason": "r", "stone_type": "Granite", "src": "marenostone",
                "scraped": "Imperial White Granite", "sources": ["marenostone"]},
               {"variant": "Imperial White", "reason": "r", "stone_type": "Quartzite", "src": "zucchi",
                "scraped": "Imperial White Quartzite", "sources": ["zucchi"]}]
    assert stage_decisions.write_confirm_file(pending) == 2
    cards = decisions_store.list_pending("variety")
    assert sorted(c["stone_type"] for c in cards) == ["Granite", "Quartzite"]
