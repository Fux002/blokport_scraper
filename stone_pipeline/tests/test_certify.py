"""Certification gate. The self-test only proves an adapter is STABLE (its golden fixture is made by
the same adapter); the vocab check proves it is CORRECT -- a swapped attribute column (e.g. finish
mapped from the colour column) is caught here. These tests cover the swap detector and assert every
configured source certifies clean (no false positives)."""

from __future__ import annotations

from stone_pipeline import certify
from stone_pipeline.adapters import REGISTRY


def test_vocab_swap_detector_flags_a_swap():
    swapped = {"color": {"n": 15, "own": 0.0, "cross": {"finish": 1.0, "type": 0.0, "quality": 0.0}}}
    msgs = certify._vocab_swaps(swapped)
    assert msgs and "swapped" in msgs[0] and "finish" in msgs[0]


def test_vocab_detector_ignores_clean_and_novel_values():
    clean = {"color": {"n": 15, "own": 1.0, "cross": {"finish": 0.0, "type": 0.0, "quality": 0.0}}}
    novel = {"color": {"n": 15, "own": 0.2, "cross": {"finish": 0.1, "type": 0.0, "quality": 0.0}}}
    assert certify._vocab_swaps(clean) == []
    assert certify._vocab_swaps(novel) == []  # resolves nowhere -> not a swap, no false positive


def test_vocab_detector_needs_enough_values():
    # too few non-empty values -> no signal, never flags (e.g. varsha's empty raw_type)
    thin = {"color": {"n": 2, "own": 0.0, "cross": {"finish": 1.0, "type": 0.0, "quality": 0.0}}}
    assert certify._vocab_swaps(thin) == []


def test_every_source_certifies_clean():
    # the whole gate (config/adapter/selftest/vocab/contract) must pass for every configured source.
    for source in REGISTRY:
        res = certify.certify_source(source)
        assert res.passed, f"{source} failed: " + "; ".join(
            f"{c.name}={c.detail}" for c in res.checks if not c.ok)


def test_check_vocab_fails_loud_when_evaluation_errors(monkeypatch):
    # F9: an ERROR during vocab evaluation (a crashing resolver, a reference/fixture bug) must FAIL the
    # check, not be swallowed as a PASS -- a swallowed error could green-light a swapped-attribute-column
    # source to auto. The genuinely-benign no-adapter/no-fixture cases return "skipped" BEFORE the try.
    from stone_pipeline.reference import loaders
    source = next(s for s in REGISTRY if (certify.fixture_dir(s) / "input.csv").exists())
    monkeypatch.setattr(loaders, "load_all", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    ok, msg = certify.check_vocab(source)
    assert ok is False and "FAILED to evaluate" in msg
