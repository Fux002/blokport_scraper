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
    decisions_store.set_origin_decision("marenostone", "Crystal White", "Granite", "IR")   # a confirmed origin
    decisions_store.set_variety_origin("Crystal White", "Granite", "IN,IR")                # a variety origin edit
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
    assert code == 200 and out["config"] == {"review_pending": 2, "attribute_ids": 1,
                                             "source_diagnostics": 0}
    assert decisions_store.list_pending("variety") == [] and decisions_store.attribute_ids() == {}
    # durable intent still survives a global reset
    assert decisions_store.confirm_map() == {"black absolute": "yes"}
    assert store.load_retired() == {"slab_granite_x_uuid"}


def test_global_reset_snapshots_both_stores_immediately_scoped_does_not(tmp_path, monkeypatch):
    """GAP 3: a global reset mutates BOTH the ledger and config.db, so it must snapshot both immediately
    (ledger then config) to shrink the cross-store tear window; a scoped reset leaves config.db alone, so it
    snapshots neither. (The helpers are no-ops off the config server; here we record the calls directly.)"""
    from stone_pipeline import lifecycle

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    _seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    monkeypatch.setattr(lifecycle, "_ledger_op", _fake_ledger_op)
    calls: list[str] = []
    monkeypatch.setattr(lifecycle, "_snapshot_ledger", lambda: calls.append("ledger"))
    monkeypatch.setattr(lifecycle, "_snapshot_config", lambda: calls.append("config"))

    lifecycle.reset(sources=["polonine"], hard=False)     # SCOPED -> config.db untouched -> no cross-store snapshot
    assert calls == []

    lifecycle.reset(sources=None, hard=False)             # GLOBAL -> snapshot BOTH, ledger first then config
    assert calls == ["ledger", "config"]


def test_pristine_reset_wipes_the_durable_operator_overlay(tmp_path, monkeypatch):
    """The factory cold start: a normal reset KEEPS operator curation, but `pristine` also forgets it, so
    the next produce derives the catalog purely from the committed seed (no mint/alias/approve/retire
    re-applies). Registered sources are left alone."""
    from stone_pipeline import lifecycle
    from stone_pipeline.ledger import snapshot
    from deploy import cleanup_images

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    _seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    monkeypatch.setattr(lifecycle, "_ledger_op", _fake_ledger_op)
    monkeypatch.setattr(cleanup_images, "wipe_all_product_images", lambda **k: {})   # global hard wipes images
    # guard the real scrape-cache wipe (rmtree + S3) behind a recorder, so the test never touches real dirs
    wiped: list[bool] = []
    monkeypatch.setattr(snapshot, "wipe_artifacts",
                        lambda **k: (wiped.append(True), {"local": ["outputs", "data"], "s3": ["k1", "k2"]})[1])

    out, code = lifecycle.reset(sources=None, pristine=True)
    assert code == 200 and out["mode"] == "pristine"
    # every clearable store reported in the response.config
    assert out["config"] == {"review_pending": 2, "attribute_ids": 1, "source_diagnostics": 0,
                             "variety_decisions": 1, "origin_decisions": 1, "variety_origins": 1,
                             "leaf_decisions": 1, "retired_keys": 1}
    # the durable overlay is GONE (unlike a normal reset), so the catalog is seed-only next produce
    assert decisions_store.confirm_map() == {}
    assert decisions_store.variety_seed_types() == {}
    assert decisions_store.backbone_leaf_overlay() == {}
    assert store.load_retired() == set()
    # the registered source survives -- scraping still works
    assert "polonine" in store.read_sources()
    # a pristine reset ALSO wipes the cached scrape (else a later republish/catalog re-mints from it)
    assert wiped == [True] and out["artifacts_wiped"]["s3"] == ["k1", "k2"]


def test_pristine_keep_images_skips_the_product_image_wipe(tmp_path, monkeypatch):
    """keep_images: a factory reset that LEAVES the hosted product images + enhanced markers (a re-scrape
    reuses them, no GPU/FAL rebuild). The rest of the factory reset still runs (overlay wipe). The default
    (test above) DOES wipe -- so this proves only that the flag skips the wipe."""
    from stone_pipeline import lifecycle
    from stone_pipeline.ledger import snapshot
    from deploy import cleanup_images

    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    _seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    monkeypatch.setattr(lifecycle, "_ledger_op", _fake_ledger_op)
    wipe_calls: list[bool] = []
    monkeypatch.setattr(cleanup_images, "wipe_all_product_images",
                        lambda **k: (wipe_calls.append(True), {})[1])
    monkeypatch.setattr(snapshot, "wipe_artifacts", lambda **k: {"local": [], "s3": []})

    out, code = lifecycle.reset(sources=None, pristine=True, keep_images=True)
    assert code == 200 and out["mode"] == "pristine"
    assert wipe_calls == []                       # the expensive product-image wipe never ran
    assert "kept" in out["images_wiped"]          # images explicitly preserved
    # the rest of the factory reset still happened -- the durable overlay is gone
    assert decisions_store.variety_seed_types() == {}


def test_hard_and_soft_reset_do_NOT_wipe_the_scrape_cache(tmp_path, monkeypatch):
    """Only a PRISTINE (factory) reset clears the cached scrape. A hard/soft reset keeps it, so a targeted
    republish still works -- wiping it there would break the "release without re-scrape" flow."""
    from stone_pipeline import lifecycle
    from stone_pipeline.ledger import snapshot
    from deploy import cleanup_images

    _seed_config(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle, "_ledger_op", _fake_ledger_op)
    monkeypatch.setattr(cleanup_images, "wipe_all_product_images", lambda **k: {})   # global hard wipes images
    wiped: list[bool] = []
    monkeypatch.setattr(snapshot, "wipe_artifacts", lambda **k: (wiped.append(True), {})[1])

    lifecycle.reset(sources=None, hard=True)          # global HARD (not pristine)
    lifecycle.reset(sources=None, hard=False)         # global SOFT
    assert wiped == []                                # neither touched the scrape cache


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


def test_pristine_reset_prunes_both_stale_medusa_exports(tmp_path, monkeypatch):
    """Regression (the ghost incidents): a factory reset must delete BOTH id-bearing Medusa exports (local AND
    S3): products_export.csv (seed_products -> ghost PRODUCTS, the 12-ghost incident) and variants_export.csv
    (seed_variations seeds each variation 'synced' WITH the file's Id column -> up to ~36k ghost VARIATIONS
    bound to dead/foreign ids; also the dev-ids-into-prod vector). Medusa is wiped on a pristine reset, so both
    are stale; removing them makes the seeders dormant until Medusa re-publishes fresh exports."""
    import dataclasses
    import boto3
    from stone_pipeline import lifecycle
    from stone_pipeline.config import settings

    prods = tmp_path / "products_export.csv"
    prods.write_text("Variant Sku,Product Handle,Inventory Quantity\nVAR-1,ghost-1,0\n", encoding="utf-8")
    variants = tmp_path / "variants_export.csv"
    variants.write_text("Id,Key,Name\nmed_1,slab_x_uuid,Ghost\n", encoding="utf-8")
    patched = dataclasses.replace(settings.SETTINGS, paths=dataclasses.replace(
        settings.SETTINGS.paths, products_known_csv=prods, variants_export_csv=variants))
    monkeypatch.setattr(settings, "SETTINGS", patched)

    deleted = []

    class _S3:
        def delete_objects(self, Bucket, Delete):
            deleted.extend(o["Key"] for o in Delete["Objects"])

    monkeypatch.setattr(boto3, "client", lambda *a, **k: _S3())

    out = lifecycle._prune_stale_medusa_export()
    assert not prods.exists() and not variants.exists()        # both local ghost sources gone
    assert out["local"] == 2 and out["s3"] == 2
    assert any(k.endswith("from_medusa/products_export.csv") for k in deleted)
    assert any(k.endswith("from_medusa/variants_export.csv") for k in deleted)


def test_unmint_removes_from_medusa_clears_decision_and_does_not_exclude(tmp_path, monkeypatch):
    """unmint = retire's removal (tombstone + cascade) MINUS the permanent exclusion PLUS clearing the mint
    decision, so the variety resurfaces UNDECIDED for a fresh call -- distinct from retire (excluded) and
    reject (never mint). Verifies it removes like retire, clears ONLY this decision, and never excludes."""
    from stone_pipeline import lifecycle

    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    decisions_store.set_variety_decision("Crystal White", "mint", seed_type="Granite")
    decisions_store.set_variety_decision("Absolute Black", "mint", seed_type="Granite")   # a bystander decision

    captured = {}

    class _Sync:
        def variation_live_products(self, lg, key):
            return 0

        def retire_variation(self, lg, key, force=False, reason="variation_removed"):
            captured["key"], captured["reason"] = key, reason
            return {"retired": key, "tombstoned_variation": True}

    class _LG:
        def get(self, table, col, key):
            return {"key": key, "name": "Crystal White", "medusa_id": "var_1"}

    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: (work(_LG(), _Sync()), 200))

    result, code = lifecycle.unmint_variation("slab_granite_crystal_white_uuid")
    assert code == 200
    assert captured["key"] == "slab_granite_crystal_white_uuid"   # removed from Medusa the same as retire
    assert captured["reason"] == "variation_unminted"
    assert result["mint_decision_cleared"] == 1                   # its mint decision is cleared -> resurfaces
    assert decisions_store.confirm_map() == {"absolute black": "yes"}   # ONLY this one cleared; bystander kept
    assert store.load_retired() == set()                         # NOT excluded (the whole point vs retire)


def test_clear_variety_decision_is_scoped_to_one_variant(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    decisions_store.set_variety_decision("Crystal White", "mint", seed_type="Granite")
    decisions_store.set_variety_decision("Absolute Black", "reject")
    assert decisions_store.clear_variety_decision("Crystal White") == 1
    assert decisions_store.clear_variety_decision("Crystal White") == 0   # idempotent: already gone
    assert decisions_store.confirm_map() == {"absolute black": "no"}      # the other decision survives
