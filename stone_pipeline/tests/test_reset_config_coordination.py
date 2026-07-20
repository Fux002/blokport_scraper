"""A GLOBAL ledger reset also clean-starts the config.db review queue + operator-pasted attribute ids, so a
clean start is coherent across BOTH stores in one call (the :4200 queue is blank immediately, no dead Medusa
id survives a wipe). A scoped per-source reset leaves config.db alone, and durable operator intent
(mint/reject/alias/seed decisions, retired keys) survives either way."""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store, store


def _seed_config(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    # a review queue (2 kinds), a pasted attribute id, a mint+seed decision, a retired key, an approved leaf
    decisions_store.replace_pending("variety", [{"ref": "black absolute", "payload": {"variant": "Black Absolute"}}])
    decisions_store.replace_pending("attribute", [{"ref": "finish:leathered", "payload": {"value": "Leathered"}}])
    decisions_store.set_attribute_id("finish", "Leathered", "pcol_9")
    decisions_store.set_variety_decision("Black Absolute", "mint", seed_type="Granite")
    decisions_store.set_backbone_leaf_decision("Black Absolute", "Granite", "color", "Gold", "approve")
    store.add_retired("slab_granite_x_uuid")


def test_clear_helpers_empty_queues_and_ids_but_keep_decisions(tmp_path, monkeypatch):
    _seed_config(tmp_path, monkeypatch)
    assert decisions_store.clear_review_pending() == 2      # variety + attribute queue rows
    assert decisions_store.clear_attribute_ids() == 1
    assert decisions_store.list_pending("variety") == []
    assert decisions_store.list_pending("attribute") == []
    assert decisions_store.attribute_ids() == {}
    # durable operator intent SURVIVES
    assert decisions_store.confirm_map() == {"black absolute": "yes"}          # mint kept
    assert decisions_store.variety_seed_types() == {"black absolute": "Granite"}
    assert store.load_retired() == {"slab_granite_x_uuid"}


def test_clear_review_pending_rejects_a_bad_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    with pytest.raises(decisions_store.InvalidDecision):
        decisions_store.clear_review_pending(("variety", "nonsense"))


def test_global_reset_clears_config_but_scoped_leaves_it(tmp_path, monkeypatch):
    from stone_pipeline import lifecycle

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  adapter: polonine\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    _seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    # the ledger reset itself is covered in test_ledger_sync; here we test only the config coordination
    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: ({"variation": 1}, 200))

    # SCOPED reset -> config.db untouched (matches the ledger reset leaving the shared base layer alone)
    out, code = lifecycle.reset(sources=["polonine"], hard=False)
    assert code == 200 and "config" not in out
    assert decisions_store.list_pending("variety") and decisions_store.attribute_ids()

    # GLOBAL reset -> queues + attribute ids cleared, reported in the response
    out, code = lifecycle.reset(sources=None, hard=False)
    assert code == 200 and out["config"] == {"review_pending": 2, "attribute_ids": 1}
    assert decisions_store.list_pending("variety") == [] and decisions_store.attribute_ids() == {}
    # durable intent still survives a global reset
    assert decisions_store.confirm_map() == {"black absolute": "yes"}
    assert store.load_retired() == {"slab_granite_x_uuid"}


def test_pristine_reset_wipes_the_durable_operator_overlay(tmp_path, monkeypatch):
    """The factory cold start: a normal reset KEEPS operator curation, but `pristine` also forgets it, so
    the next produce derives the catalog purely from the committed seed (no mint/alias/approve/retire
    re-applies). Registered sources are left alone."""
    from stone_pipeline import lifecycle

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  adapter: polonine\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    _seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: ({"variation": 1}, 200))

    out, code = lifecycle.reset(sources=None, pristine=True)
    assert code == 200 and out["mode"] == "pristine"
    # every clearable store reported in the response.config
    assert out["config"] == {"review_pending": 2, "attribute_ids": 1,
                             "variety_decisions": 1, "leaf_decisions": 1, "retired_keys": 1}
    # the durable overlay is GONE (unlike a normal reset), so the catalog is seed-only next produce
    assert decisions_store.confirm_map() == {}
    assert decisions_store.variety_seed_types() == {}
    assert decisions_store.backbone_leaf_overlay() == {}
    assert store.load_retired() == set()
    # the registered source survives -- scraping still works
    assert "polonine" in store.read_sources()


def test_pristine_reset_is_global_only(tmp_path, monkeypatch):
    from stone_pipeline import lifecycle
    _seed_config(tmp_path, monkeypatch)
    out, code = lifecycle.reset(sources=["polonine"], pristine=True)
    assert code == 400 and "global-only" in out["error"]
    # nothing was touched: the durable overlay is intact
    assert decisions_store.confirm_map() == {"black absolute": "yes"}
    assert store.load_retired() == {"slab_granite_x_uuid"}


def test_a_refused_ledger_reset_does_not_touch_config(tmp_path, monkeypatch):
    from stone_pipeline import lifecycle

    _seed_config(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: ({"error": "in flight"}, 409))
    out, code = lifecycle.reset(sources=None)
    assert code == 409
    # a 409 (a pull is mid-flight) must leave the config queue + ids intact
    assert decisions_store.list_pending("variety") and decisions_store.attribute_ids()
