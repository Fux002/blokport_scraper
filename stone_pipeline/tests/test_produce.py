"""produce.py orchestration: the full from-scratch produce = fetch export inputs -> LIVE scrape ->
build. This is what the `/run` trigger runs so a produce on a fresh host (empty data/) populates from
nothing. The order matters (scrape before build), catalog-only must NOT scrape (it re-consolidates
existing outputs), a failed scrape must abort before build, and fetch_inputs is best-effort (a fetch
failure never blocks the produce)."""

from __future__ import annotations

from stone_pipeline import produce


def _stub(monkeypatch):
    """Record the produce steps in order; stub build.main so nothing touches disk/network."""
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append(("fetch", None)))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append(("scrape", s)) or 0)
    monkeypatch.setattr(produce.build, "main", lambda argv: calls.append(("build", argv)) or 0)
    return calls


def test_full_produce_fetches_then_scrapes_then_builds(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--sources", "zucchi"]) == 0
    assert calls == [("fetch", None), ("scrape", ["zucchi"]), ("build", ["--sources", "zucchi"])]


def test_default_scope_scrapes_all(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main([]) == 0
    assert calls == [("fetch", None), ("scrape", None), ("build", [])]


def test_finalize_skips_inventory_but_runs_for_a_validated_produce(monkeypatch):
    # an inventory run is a stock refresh, not a validated produce -- it must NOT persist a diagnostic or
    # record an admission event (which would advance the N-clean-runs-to-auto streak without validating).
    from stone_pipeline.config import admission, diagnostics
    calls = []
    monkeypatch.setattr(diagnostics, "persist_from_outputs", lambda *a, **k: calls.append("persist"))
    monkeypatch.setattr(admission, "evaluate_from_outputs", lambda *a, **k: calls.append("evaluate"))
    produce._finalize_control_plane("inventory")
    assert calls == []                                   # inventory: no control-plane side effects
    produce._finalize_control_plane("all")
    assert calls == ["persist", "evaluate"]              # a validated produce records both


def test_catalog_only_never_scrapes(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--stage", "catalog"]) == 0
    assert calls == [("fetch", None), ("build", ["--stage", "catalog"])]   # fetch, then straight to build


def test_inventory_stage_still_scrapes_first(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--stage", "inventory"]) == 0
    assert calls == [("fetch", None), ("scrape", None), ("build", ["--stage", "inventory"])]


def test_verify_delegates_straight_to_build(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--verify"]) == 0
    assert calls == [("build", ["--verify"])]        # no fetch, no scrape


def test_failed_scrape_aborts_before_build(monkeypatch):
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append("fetch"))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append("scrape") or 3)
    monkeypatch.setattr(produce.build, "main", lambda argv: calls.append("build") or 0)
    assert produce.main(["--sources", "zucchi"]) == 3
    assert calls == ["fetch", "scrape"]              # build never runs on a failed scrape


def test_fetch_inputs_failure_is_best_effort(monkeypatch):
    """A fetch failure logs and continues -- it must not raise out of the produce."""
    def boom():
        raise RuntimeError("no S3 here")
    import stone_pipeline.produce as p
    monkeypatch.setattr("deploy.fetch_inputs.main", boom, raising=False)
    # call the real _fetch_inputs (not the stub) to prove it swallows the error
    p._fetch_inputs()   # must not raise


# -- pull-model gate reconciliation (Medusa's scoped non-fatal-for-new-varieties) -----------------

NEW_VARIETY_ERRORS = ["225 product variation ids are NOT in the current export (stale products ...)",
                      "225 product variations have NO valid-combination row (unpriceable ...)"]


def _reconcile(monkeypatch, errors, held, untyped):
    from stone_pipeline import catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "verify_consistency", lambda: (errors, []))
    monkeypatch.setattr(produce, "_ledger_gate_state", lambda: (held, untyped))
    return produce._reconcile_gate(1)


def test_gate_held_when_only_new_varieties(monkeypatch):
    # only new-variety errors + new varieties explain them -> held, exit 0
    assert _reconcile(monkeypatch, NEW_VARIETY_ERRORS, held=225, untyped=0) == 0


def test_gate_held_even_with_untyped_new_varieties(monkeypatch):
    # untyped new varieties are informational -- the sync engine holds them, not a produce failure
    assert _reconcile(monkeypatch, NEW_VARIETY_ERRORS, held=225, untyped=3) == 0


def test_gate_stays_fatal_when_no_new_varieties_explain_it(monkeypatch):
    # gate failed but the ledger has no held new varieties -> not the two-pass checkpoint -> fatal
    assert _reconcile(monkeypatch, NEW_VARIETY_ERRORS, held=0, untyped=0) == 1


def test_gate_stays_fatal_on_error_outside_the_new_variety_class(monkeypatch):
    errs = NEW_VARIETY_ERRORS + ["12 inventory SKUs are NOT in the Medusa product export ..."]
    assert _reconcile(monkeypatch, errs, held=225, untyped=0) == 1


def test_gate_keeps_failure_if_no_errors_surface(monkeypatch):
    # build failed for some non-gate reason (verify_consistency clean) -> keep the original failure
    assert _reconcile(monkeypatch, [], held=225, untyped=0) == 1
