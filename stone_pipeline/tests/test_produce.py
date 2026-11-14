"""produce.py orchestration: the full from-scratch produce = fetch export inputs -> LIVE scrape ->
build. This is what the `/run` trigger runs so a produce on a fresh host (empty data/) populates from
nothing. The order matters (scrape before build), catalog-only must NOT scrape (it re-consolidates
existing outputs), a failed scrape must abort before build, and fetch_inputs is best-effort only in
development (a real S3 failure aborts a production produce; an empty prefix is never a failure)."""

from __future__ import annotations

import pytest

from stone_pipeline import produce


def _stub(monkeypatch):
    """Record the produce steps in order; stub build.run so nothing touches disk/network."""
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append(("fetch", None)))
    # VE2: the archived mirror runs right after fetch, before any source is recorded -- record it so the
    # fetch -> mirror -> scrape -> build order is PROTECTED (deleting or moving the call fails these tests).
    monkeypatch.setattr(produce, "_mirror_archived", lambda: calls.append(("mirror", None)))
    # record the frozen-waiting log as a step too, so the order tests PROTECT its call site (after build):
    # deleting it from main fails them instead of silently passing.
    monkeypatch.setattr(produce, "_log_frozen_waiting", lambda: calls.append(("frozen_log", None)))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append(("scrape", s)) or 0)
    # the supplier prices this scrape read are published right after it, before the build (market_prices.supplier)
    monkeypatch.setattr(produce, "_emit_supplier_prices", lambda s, since: calls.append(("supplier_prices", s)))
    monkeypatch.setattr(produce.build, "run", lambda sources, stage: calls.append(("build", sources, stage)) or 0)
    # persist + publish belong to produce now (wave 4g); off for these step-order tests
    from stone_pipeline.ledger import snapshot
    from stone_pipeline.io import deliverables as upload_artifacts
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(upload_artifacts, "upload_all", lambda run_id=None: 0)
    return calls


def test_full_produce_fetches_then_scrapes_then_builds(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.run(["zucchi"], "all") == 0
    assert calls == [("fetch", None), ("mirror", None), ("scrape", ["zucchi"]), ("supplier_prices", ["zucchi"]),
                     ("build", ["zucchi"], "all"), ("frozen_log", None)]


def test_default_scope_scrapes_all(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.run(None, "all") == 0
    assert calls == [("fetch", None), ("mirror", None), ("scrape", None), ("supplier_prices", None),
                     ("build", None, "all"), ("frozen_log", None)]


def test_finalize_skips_inventory_but_runs_for_a_validated_produce(monkeypatch):
    # an inventory run is a stock refresh, not a validated produce -- it must NOT persist a diagnostic or
    # record an admission event (which would advance the N-clean-runs-to-auto streak without validating).
    from stone_pipeline.config.control import admission, diagnostics
    calls = []
    monkeypatch.setattr(diagnostics, "persist_from_outputs", lambda *a, **k: calls.append("persist"))
    monkeypatch.setattr(admission, "evaluate_from_outputs", lambda *a, **k: calls.append("evaluate"))
    produce._finalize_control_plane("inventory")
    assert calls == []                                   # inventory: no control-plane side effects
    produce._finalize_control_plane("all")
    assert calls == ["persist", "evaluate"]              # a validated produce records both


def test_catalog_only_never_scrapes(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.run(None, "catalog") == 0
    # fetch, mirror, then straight to build (no scrape); the frozen-waiting log runs after build
    assert calls == [("fetch", None), ("mirror", None), ("build", None, "catalog"),
                     ("frozen_log", None)]


def test_republish_stage_never_scrapes_but_still_mirrors(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.run(None, "republish") == 0
    assert calls == [("fetch", None), ("mirror", None), ("build", None, "republish"),
                     ("frozen_log", None)]


def test_inventory_stage_still_scrapes_first(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.run(None, "inventory") == 0
    assert calls == [("fetch", None), ("mirror", None), ("scrape", None), ("supplier_prices", None),
                     ("build", None, "inventory"), ("frozen_log", None)]


def test_verify_delegates_straight_to_build(monkeypatch):
    calls = _stub(monkeypatch)
    monkeypatch.setattr(produce.build, "verify_gate", lambda: calls.append(("verify",)) or 0)
    assert produce.run(None, "all", verify=True) == 0
    assert calls == [("verify",)]        # no fetch, no scrape


def test_failed_scrape_aborts_before_build(monkeypatch):
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append("fetch"))
    monkeypatch.setattr(produce, "_mirror_archived", lambda: calls.append("mirror"))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append("scrape") or 3)
    monkeypatch.setattr(produce.build, "run", lambda sources, stage: calls.append("build") or 0)
    assert produce.run(["zucchi"], "all") == 3
    assert calls == ["fetch", "mirror", "scrape"]    # build never runs on a failed scrape; mirror precedes it


def test_mirror_archived_propagates_a_ledger_failure(monkeypatch):
    # the fetch-time mirror must FAIL LOUD: if the ledger cannot be opened, the produce raises rather than
    # continuing to scrape (which would record new products on a freshly archived variety). Restoring a
    # try/except around it would make this test fail.
    from stone_pipeline.ledger import writethrough

    def _boom(*a, **k):
        raise RuntimeError("ledger busy past busy_timeout")
    monkeypatch.setattr(writethrough, "enabled", lambda: True)
    monkeypatch.setattr(writethrough, "open_ledger", _boom)
    with pytest.raises(RuntimeError):
        produce._mirror_archived()


def test_frozen_log_skipped_when_the_build_fails(monkeypatch):
    # the frozen-waiting log must NOT run on a failed build: it would open the ledger and could replace the
    # build's own exit code (a consistency-gate rc, an abort) with a traceback. A failed build returns its rc.
    calls = _stub(monkeypatch)
    monkeypatch.setattr(produce.build, "run", lambda sources, stage: calls.append(("build", sources, stage)) or 7)
    assert produce.run(["zucchi"], "all") == 7
    assert ("frozen_log", None) not in calls
    assert calls == [("fetch", None), ("mirror", None), ("scrape", ["zucchi"]), ("supplier_prices", ["zucchi"]),
                     ("build", ["zucchi"], "all")]


def test_fetch_inputs_failure_is_best_effort(monkeypatch):
    """A fetch failure logs and continues -- it must not raise out of the produce."""
    def boom():
        raise RuntimeError("no S3 here")
    import stone_pipeline.produce as p
    monkeypatch.setattr("stone_pipeline.io.medusa_inputs.fetch_all", boom, raising=False)
    # the field-kinds fetch is fatal on every env (test_field_kinds); this test is about the export fetch only
    monkeypatch.setattr("stone_pipeline.io.medusa_inputs.fetch_field_kinds", lambda: None)
    monkeypatch.setattr("stone_pipeline.io.medusa_inputs.fetch_overrides", lambda: False)   # fatal too (its own test)
    # call the real _fetch_inputs (not the stub) to prove it swallows the error
    p._fetch_inputs()   # must not raise


def test_frozen_waiting_is_logged_when_products_wait(monkeypatch):
    # VE2 finding 4: the frozen-waiting line must be logged on every produce stage (write-through on), and
    # skipped when nothing waits. Spy on the logger so this does not depend on log propagation.
    from stone_pipeline.ledger import writethrough
    monkeypatch.setattr(writethrough, "enabled", lambda: True)
    monkeypatch.setattr(produce, "_frozen_waiting", lambda: 2)
    seen: list = []
    monkeypatch.setattr(produce.log, "warning", lambda msg, **k: seen.append((msg, k)))
    produce._log_frozen_waiting()
    assert len(seen) == 1 and seen[0][1]["extra"]["extra_fields"]["frozen_waiting"] == 2
    seen.clear()
    monkeypatch.setattr(produce, "_frozen_waiting", lambda: 0)   # nothing waiting -> no line
    produce._log_frozen_waiting()
    assert seen == []


def test_frozen_waiting_is_silent_when_writethrough_is_off(monkeypatch):
    from stone_pipeline.ledger import writethrough
    monkeypatch.setattr(writethrough, "enabled", lambda: False)
    called: list = []
    monkeypatch.setattr(produce, "_frozen_waiting", lambda: called.append(1) or 5)
    produce._log_frozen_waiting()
    assert called == []   # write-through off: never opens the ledger to count


# -- pull-model gate reconciliation (Medusa's scoped non-fatal-for-new-varieties) -----------------

NEW_VARIETY_ERRORS = ["225 product variation ids are NOT in the current export (stale products ...)",
                      "225 product variations have NO valid-combination row (unpriceable ...)"]


def _reconcile(monkeypatch, errors, held, untyped):
    from stone_pipeline import catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "verify_consistency", lambda: (errors, []))
    monkeypatch.setattr(produce, "_ledger_gate_state", lambda: (held, untyped))
    return produce._reconcile_gate(catalog_mod.CONSISTENCY_GATE_RC)   # the gate's own failure code


def test_gate_held_when_only_new_varieties(monkeypatch):
    # only new-variety errors + new varieties explain them -> held, exit 0
    assert _reconcile(monkeypatch, NEW_VARIETY_ERRORS, held=225, untyped=0) == 0


def test_gate_held_even_with_untyped_new_varieties(monkeypatch):
    # untyped new varieties are informational -- the sync engine holds them, not a produce failure
    assert _reconcile(monkeypatch, NEW_VARIETY_ERRORS, held=225, untyped=3) == 0


def test_gate_stays_fatal_when_no_new_varieties_explain_it(monkeypatch):
    # gate failed but the ledger has no held new varieties -> not the two-pass checkpoint -> fatal
    assert _reconcile(monkeypatch, NEW_VARIETY_ERRORS, held=0, untyped=0) != 0


def test_gate_stays_fatal_on_error_outside_the_new_variety_class(monkeypatch):
    errs = NEW_VARIETY_ERRORS + ["12 inventory SKUs are NOT in the Medusa product export ..."]
    assert _reconcile(monkeypatch, errs, held=225, untyped=0) != 0


def test_gate_keeps_failure_if_no_errors_surface(monkeypatch):
    # build failed for some non-gate reason (verify_consistency clean) -> keep the original failure
    assert _reconcile(monkeypatch, [], held=225, untyped=0) != 0


# empty-export cold start: verify_consistency returns ONLY the "cannot verify" error (nothing to check
# against yet). tree_build already treats this as a soft cold start; the gate must too -- rescued when the
# ledger confirms new varieties, fatal otherwise (a warm run whose export genuinely broke with nothing new).
EMPTY_EXPORT_ERROR = ["variants_export.csv is missing or has no Ids -- cannot verify"]


def test_gate_held_on_empty_export_cold_start_with_new_varieties(monkeypatch):
    assert _reconcile(monkeypatch, EMPTY_EXPORT_ERROR, held=12181, untyped=0) == 0


def test_gate_stays_fatal_on_empty_export_when_nothing_new_explains_it(monkeypatch):
    # export empty but the ledger has no held new varieties -> a genuinely broken warm export -> fatal
    assert _reconcile(monkeypatch, EMPTY_EXPORT_ERROR, held=0, untyped=0) != 0


def test_fetch_inputs_failure_is_fatal_in_production_and_best_effort_in_development(monkeypatch):
    # An empty S3 prefix is NOT an error (medusa_inputs.fetch_all returns 0 with "no input files"), so failing loud
    # on an exception cannot block a cold start. A real S3/credential failure in production must abort the
    # produce: matching against a stale or absent export would hold every variety and exit 0.
    from stone_pipeline.io import medusa_inputs as fetch_inputs
    from stone_pipeline.config import categories

    def _boom():
        raise RuntimeError("s3 unreachable")
    monkeypatch.setattr(fetch_inputs, "fetch_all", _boom)
    # the field-kinds fetch succeeds here, so the only exit is the export fetch's own (its failure: test_field_kinds)
    monkeypatch.setattr(fetch_inputs, "fetch_field_kinds", lambda: None)
    # and so does the overrides fetch (its failure is fatal on every env: test_dimension_overrides)
    monkeypatch.setattr(fetch_inputs, "fetch_overrides", lambda: False)
    monkeypatch.setattr(categories, "refresh_category_pcats", lambda: None)
    monkeypatch.setattr(produce, "IS_PRODUCTION", True, raising=False)
    with pytest.raises(SystemExit):
        produce._fetch_inputs()
    monkeypatch.setattr(produce, "IS_PRODUCTION", False, raising=False)
    produce._fetch_inputs()                                  # a laptop without S3 keeps producing


def test_only_a_consistency_gate_failure_is_reconciled(monkeypatch):
    # A write-through abort, a scrape-floor abort or any other non-gate failure must stay fatal even when the
    # consistency errors happen to be the cold-start class and new varieties are held: those two facts are
    # the normal state of a new-variety produce, so they must never excuse an unrelated failure.
    from stone_pipeline import catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "verify_consistency", lambda: (NEW_VARIETY_ERRORS, []))
    monkeypatch.setattr(produce, "_ledger_gate_state", lambda: (225, 0))
    assert produce._reconcile_gate(1) == 1                                   # generic failure: fatal
    assert produce._reconcile_gate(2) == 2                                   # a run/write-through abort: fatal
    assert produce._reconcile_gate(catalog_mod.CONSISTENCY_GATE_RC) == 0    # the gate itself: reconciled


def test_consistency_gate_failure_exits_with_its_own_code(monkeypatch):
    from stone_pipeline import catalog as catalog_mod
    monkeypatch.setattr(catalog_mod, "verify_consistency", lambda: (["x is NOT in the current export"], []))
    with pytest.raises(SystemExit) as exc:
        catalog_mod._consistency_gate()
    assert exc.value.code == catalog_mod.CONSISTENCY_GATE_RC
    monkeypatch.setattr(catalog_mod, "verify_consistency", lambda: ([], ["a warning"]))
    catalog_mod._consistency_gate()                                          # warnings never abort


# --- supplier prices: published from this produce's own scrape, isolated from the product produce ---------------------
def test_a_supplier_price_failure_never_fails_the_product_produce(monkeypatch):
    from market_prices import supplier
    from market_prices.register import RegisterError

    def broken(sources, since):
        raise RegisterError("the register does not validate")
    seen = []
    monkeypatch.setattr(supplier, "emit_live", broken)
    monkeypatch.setattr(produce.log, "error", lambda msg, **k: seen.append(msg))
    produce._emit_supplier_prices(["polonine"], "20261114_120000")      # no raise: the products need no price
    assert seen and "supplier prices" in seen[0]


def test_supplier_prices_read_only_scrapes_written_by_this_produce(monkeypatch):
    calls = _stub(monkeypatch)
    stamps = []
    monkeypatch.setattr(produce, "_emit_supplier_prices", lambda s, since: stamps.append(since))
    produce.run(["polonine"], "all")
    assert len(stamps) == 1 and len(stamps[0]) == len("20261114_120000")   # the scrape folders' own stamp form
    assert ("scrape", ["polonine"]) in calls
