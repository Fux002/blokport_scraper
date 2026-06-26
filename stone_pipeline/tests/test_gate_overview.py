"""Build-level gate surface: a per-source gate table across a multi-source run, and
the trust-ladder guard that refuses to let a `mode: auto` source auto-load when its
gates did not pass. This is what tells you, when onboarding a new source, whether it
fed through properly -- module by module -- instead of discovering it at upload.
"""

from __future__ import annotations

from stone_pipeline import run
from stone_pipeline.core.manifest import Manifest


def _manifest(gate_status: dict[str, str]) -> Manifest:
    m = Manifest(run_id="r", source="s", code_version="v", environment="e")
    m.gate_status = gate_status
    return m


def test_overview_lists_every_requested_source_and_marks_aborted():
    results = {"varsha": _manifest({"ingest": "OK", "clean": "OK", "process": "OK"})}
    text = "\n".join(run.gate_overview_lines(results, ["varsha", "zucchi"]))
    assert "varsha" in text and "ingest=OK clean=OK process=OK" in text
    assert "zucchi" in text and "ABORTED before emit" in text  # absent from results


def test_auto_source_with_clean_gates_is_not_blocked():
    results = {"x": _manifest({"ingest": "OK", "clean": "OK", "process": "OK"})}
    blocked = run.auto_sources_blocked(results, ["x"], mode_lookup=lambda s: "auto")
    assert blocked == []


def test_auto_source_with_failed_gate_is_blocked():
    results = {"x": _manifest({"ingest": "OK", "clean": "OK", "process": "FAILED"})}
    blocked = run.auto_sources_blocked(results, ["x"], mode_lookup=lambda s: "auto")
    assert blocked == [("x", "gate(s) FAILED: process")]


def test_auto_source_that_aborted_is_blocked():
    blocked = run.auto_sources_blocked({}, ["x"], mode_lookup=lambda s: "auto")
    assert blocked == [("x", "aborted before emit")]


def test_review_source_is_never_blocked_even_when_failed():
    # a review-mode source is already quarantined; the auto guard leaves it alone.
    results = {"x": _manifest({"ingest": "OK", "clean": "OK", "process": "FAILED"})}
    blocked = run.auto_sources_blocked(results, ["x"], mode_lookup=lambda s: "review")
    assert blocked == []
