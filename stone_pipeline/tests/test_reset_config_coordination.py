"""A GLOBAL ledger reset also clean-starts the config.db review queue + operator-pasted attribute ids, so a
clean start is coherent across BOTH stores in one call (the :4200 queue is blank immediately, no dead Medusa
id survives a wipe). A scoped per-source reset leaves config.db alone, and durable operator intent
(mint/reject/alias/seed decisions, retired keys) survives either way."""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store, store


def _fake_ledger_op(name, work):
    """Invoke `work` with a stub ledger+sync so the reset's config-clear + image-wipe (which now run INSIDE
    the exclusive slot, via _do_reset) actually execute, without opening a real ledger. A test that wants to
    simulate a REFUSED op (409) returns without calling work instead."""
    class _Sync:
        def reset_sync_state(self, lg, source_codes=None, hard=False, prune_stale=False):
            return {"variation": 1}

        def reconcile_variations_to_seed(self, lg, seed_keys, protected=None, seed_identities=None):
            return {}
    return work(None, _Sync()), 200


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
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    _seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    # the ledger reset itself is covered in test_ledger_sync; here we test only the config coordination
    monkeypatch.setattr(lifecycle, "_ledger_op", _fake_ledger_op)

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
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    _seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    monkeypatch.setattr(lifecycle, "_ledger_op", _fake_ledger_op)

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


def test_publish_base_to_import_targets_the_to_upload_namespace(monkeypatch):
    # bulk-restore phase 1: the base must land at the IMPORT key Medusa's /restore reads, NOT from_medusa/.
    import boto3
    from pathlib import Path
    from stone_pipeline.reference import sync_variants_base

    seen: dict = {}

    class _S3:
        def upload_file(self, path, bucket, key):
            seen.update(path=path, bucket=bucket, key=key)

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _S3())
    assert sync_variants_base.publish_base_to_import(Path("/tmp/variants_export_base.csv")) is True
    assert seen["key"].endswith("scraper/to_upload/variants_export_base.csv"), seen["key"]


def test_pristine_reseed_publishes_the_seed_to_the_import_namespace(tmp_path, monkeypatch):
    # the pristine reseed publishes the SEED to BOTH from_medusa/ (matcher) and to_upload/ (Medusa restore).
    from stone_pipeline import lifecycle
    from stone_pipeline.reference import sync_variants_base

    import_calls: list = []
    monkeypatch.setattr(sync_variants_base, "publish_base_to_s3", lambda p: True)
    monkeypatch.setattr(sync_variants_base, "publish_base_to_import",
                        lambda p: (import_calls.append(str(p)), True)[1])
    seed = tmp_path / "seed.csv"
    seed.write_text("Key,Name\nslab_marble_x_1,X\n", encoding="utf-8")
    base = tmp_path / "base.csv"

    r = lifecycle._reseed_base_from_pristine(seed_path=seed, base_path=base)
    assert r["reseeded"] and r.get("import_published") is True
    assert import_calls == [str(base)], "reset must publish the reseeded base to the import namespace"


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


def test_hard_reset_wipes_images_scoped_per_source_soft_keeps_them(tmp_path, monkeypatch):
    """A HARD reset ('Remove data (keep config)') wipes the hosted product images -- ONLY the named source's
    when scoped, all when global; a SOFT reset keeps them (the cheap reuse-images restart)."""
    from stone_pipeline import lifecycle
    from deploy import cleanup_images

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("varsha:\n  source_code: var\n  vendor: V\n"
                         "zucchi:\n  source_code: zuc\n  vendor: Z\n", encoding="utf-8")
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.seed_from_yaml(yaml_path=yaml_path)
    monkeypatch.setattr(lifecycle, "_ledger_op", _fake_ledger_op)

    wiped_sources: list[str] = []
    wiped_global: list[bool] = []
    monkeypatch.setattr(cleanup_images, "wipe_source_product_images",
                        lambda s, **k: (wiped_sources.append(s), {"improved": 1})[1])
    monkeypatch.setattr(cleanup_images, "wipe_all_product_images",
                        lambda **k: (wiped_global.append(True), {})[1])

    # SOFT scoped reset -> images KEPT
    lifecycle.reset(sources=["varsha"], hard=False)
    assert wiped_sources == [] and wiped_global == []

    # HARD scoped reset -> wipes ONLY varsha's images, never the global path
    out, code = lifecycle.reset(sources=["varsha"], hard=True)
    assert code == 200 and wiped_sources == ["varsha"] and wiped_global == []
    assert out["images_wiped"] == {"varsha": {"improved": 1}}

    # HARD global reset -> wipes ALL (single global call, no per-source)
    wiped_sources.clear()
    lifecycle.reset(sources=None, hard=True)
    assert wiped_global == [True] and wiped_sources == []


def test_reset_rejects_an_explicit_empty_sources_list(tmp_path, monkeypatch):
    """[] is neither global (null/omit) nor scoped (named) -- almost always a UI bug that would otherwise
    trigger a full global reset+wipe. Refuse it, and touch nothing."""
    from stone_pipeline import lifecycle
    _seed_config(tmp_path, monkeypatch)
    out, code = lifecycle.reset(sources=[], hard=True)
    assert code == 400 and "empty list" in out["error"]
    assert decisions_store.list_pending("variety") and decisions_store.attribute_ids()   # nothing cleared
