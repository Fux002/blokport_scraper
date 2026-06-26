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
