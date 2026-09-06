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


def _stub_ledger_and_sync(monkeypatch, captured, live=0):
    """A hermetic ledger + sync for the VARIETY-scoped unmint: one variety 'Crystal White' (Granite) with its
    three category Keys, plus a bystander variety. variety_identity is stubbed to the branch-free rule so
    the sibling grouping is deterministic without the type vocab."""
    from stone_pipeline import lifecycle
    from stone_pipeline.reference import loaders
    rows = [
        {"key": "slab_granite_crystal_white_a", "name": "Crystal White"},
        {"key": "block_granite_crystal_white_b", "name": "Crystal White"},
        {"key": "tile_granite_crystal_white_c", "name": "Crystal White"},
        {"key": "slab_granite_absolute_black_d", "name": "Absolute Black"},
    ]
    monkeypatch.setattr(loaders, "variety_identity",
                        lambda key, name: (key.split("_")[0], "granite", name.lower().replace(" ", "_")))

    class _Sync:
        def _lock_and_check_in_flight(self, lg):
            pass

        def variation_live_products(self, lg, key):
            return live

        def retire_variation(self, lg, key, force=False, reason="variation_removed"):
            captured.setdefault("retired", []).append(key)
            captured["reason"] = reason
            return {"retired": key}

    class _LG:
        def execute(self, sql):
            return type("C", (), {"fetchall": staticmethod(lambda: rows)})()

    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: (work(_LG(), _Sync()), 200))


def test_unmint_removes_the_whole_variety_clears_decision_once_and_does_not_exclude(tmp_path, monkeypatch):
    """unmint is VARIETY-scoped: ONE Key removes ALL its category siblings (block/slab/tile), clears the mint
    decision ONCE, and never adds the retired-exclusion -- so it resurfaces UNDECIDED (vs retire = excluded,
    reject = never mint). A single Key can no longer half-remove a variety."""
    from stone_pipeline import lifecycle
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    decisions_store.set_variety_decision("Crystal White", "mint", seed_type="Granite")
    decisions_store.set_variety_decision("Absolute Black", "mint", seed_type="Granite")   # bystander
    captured = {}
    _stub_ledger_and_sync(monkeypatch, captured)

    result, code = lifecycle.unmint_variation("slab_granite_crystal_white_a")       # ONE Key in...
    assert code == 200
    assert sorted(captured["retired"]) == ["block_granite_crystal_white_b",           # ...ALL three siblings out
                                          "slab_granite_crystal_white_a", "tile_granite_crystal_white_c"]
    assert captured["reason"] == "variation_unminted"
    assert result["variety_count"] == 1 and result["unminted_count"] == 3
    assert result["mint_decisions_cleared"] == 1                                      # cleared ONCE per variety
    assert decisions_store.confirm_map() == {"absolute black": "yes"}                 # bystander untouched
    assert store.load_retired() == set()                                             # NOT excluded


def test_clear_variety_decision_is_scoped_to_one_variant(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    decisions_store.set_variety_decision("Crystal White", "mint", seed_type="Granite")
    decisions_store.set_variety_decision("Absolute Black", "reject")
    assert decisions_store.clear_variety_decision("Crystal White") == 1
    assert decisions_store.clear_variety_decision("Crystal White") == 0   # idempotent: already gone
    assert decisions_store.confirm_map() == {"absolute black": "no"}      # the other decision survives


def test_bulk_unmint_collapses_siblings_to_one_variety_and_is_best_effort(tmp_path, monkeypatch):
    """Bulk: Keys collapse to distinct varieties (two sibling Keys -> the variety removed ONCE, all three
    siblings out); an unknown Key lands in `skipped` and the rest proceed; partial success is 200."""
    from stone_pipeline import lifecycle
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    captured = {}
    _stub_ledger_and_sync(monkeypatch, captured)
    result, code = lifecycle.unmint_variations(
        ["slab_granite_crystal_white_a", "block_granite_crystal_white_b", "bad_key"])
    assert code == 200
    assert result["requested"] == 3 and result["variety_count"] == 1 and result["unminted_count"] == 3
    assert result["skipped"] == [{"key": "bad_key", "code": 404, "error": "unknown variation 'bad_key'"}]


def test_unmint_reports_an_honest_status_when_nothing_was_removed(tmp_path, monkeypatch):
    """#3: nothing removed must NOT look like success -- all unknown -> 404; live products with no force ->
    409 (and force cascades to 200)."""
    from stone_pipeline import lifecycle
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    _stub_ledger_and_sync(monkeypatch, {})
    assert lifecycle.unmint_variations(["nope_1", "nope_2"])[1] == 404
    _stub_ledger_and_sync(monkeypatch, {}, live=2)                                   # variety has live products
    result, code = lifecycle.unmint_variation("slab_granite_crystal_white_a")
    assert code == 409 and result["variety_count"] == 0 and result["skipped"][0]["code"] == 409
    assert lifecycle.unmint_variation("slab_granite_crystal_white_a", force=True)[1] == 200


def test_bulk_unmint_rejects_empty_or_invalid_keys(tmp_path, monkeypatch):
    from stone_pipeline import lifecycle
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    assert lifecycle.unmint_variations([])[1] == 400
    assert lifecycle.unmint_variations(None)[1] == 400
    assert lifecycle.unmint_variations(["ok", ""])[1] == 400     # any blank Key invalidates the request


def test_list_minted_variations_groups_non_base_keys(monkeypatch):
    """The minted list = ledger varieties whose Key is NOT in the committed base, grouped by variety with
    its category Keys (the key-bearing surface Blokport needs for checkbox-select unmint)."""
    from stone_pipeline import lifecycle
    from stone_pipeline.ledger import writethrough
    import stone_pipeline.ledger.db as db

    monkeypatch.setattr(lifecycle, "_committed_base_keys", lambda: {"slab_granite_base_x"})
    monkeypatch.setattr(writethrough, "ledger_path", lambda: "/tmp/x.db", raising=False)
    monkeypatch.setattr(writethrough, "backend_fingerprint", lambda: "fp", raising=False)
    rows = [
        {"key": "slab_granite_base_x", "name": "Base Stone", "type": "Granite"},        # in base -> excluded
        {"key": "slab_quartzite_ocean_blue_a", "name": "Ocean Blue", "type": "Quartzite"},
        {"key": "block_quartzite_ocean_blue_b", "name": "Ocean Blue", "type": "Quartzite"},
        {"key": "slab_crystal_lumiere_c", "name": "Lumiere", "type": "Crystal"},
    ]

    class _LG:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql): return type("C", (), {"fetchall": staticmethod(lambda: rows)})()

    monkeypatch.setattr(db.Ledger, "open", classmethod(lambda cls, *a, **k: _LG()))
    result, code = lifecycle.list_minted_variations()
    assert code == 200
    assert result["variety_count"] == 2 and result["key_count"] == 3
    assert {v["variety"] for v in result["minted"]} == {"Ocean Blue", "Lumiere"}
    ob = next(v for v in result["minted"] if v["variety"] == "Ocean Blue")
    assert sorted(ob["keys"]) == ["block_quartzite_ocean_blue_b", "slab_quartzite_ocean_blue_a"]


def test_list_minted_variations_refuses_without_a_committed_base(monkeypatch):
    from stone_pipeline import lifecycle
    monkeypatch.setattr(lifecycle, "_committed_base_keys", lambda: set())
    assert lifecycle.list_minted_variations()[1] == 503     # never lists the whole catalogue as 'minted'


def test_unmint_all_minted_derives_keys_from_the_minted_list(monkeypatch):
    """all_minted mode: the scraper finds the non-base Keys itself (list_minted_variations) and unmints
    them all -- no paste. A no-op when nothing is minted; refuses (503) if the minted list can't be built."""
    from stone_pipeline import lifecycle
    monkeypatch.setattr(lifecycle, "list_minted_variations", lambda: (
        {"minted": [{"variety": "Ocean Blue", "stone_type": "Quartzite",
                     "keys": ["slab_q_ocean_a", "block_q_ocean_b"]},
                    {"variety": "Lumiere", "stone_type": "Crystal", "keys": ["slab_c_lumiere_c"]}],
         "variety_count": 2, "key_count": 3}, 200))
    got = {}
    monkeypatch.setattr(lifecycle, "unmint_variations",
                        lambda keys, force=False: (got.update(keys=keys, force=force) or
                                                   {"unminted": keys, "unminted_count": len(keys)}, 200))
    result, code = lifecycle.unmint_all_minted(force=True)
    assert code == 200
    assert got["keys"] == ["slab_q_ocean_a", "block_q_ocean_b", "slab_c_lumiere_c"] and got["force"] is True
    assert result["source"] == "all_minted"

    # no minted varieties -> clean no-op, never calls unmint
    monkeypatch.setattr(lifecycle, "list_minted_variations",
                        lambda: ({"minted": [], "variety_count": 0, "key_count": 0}, 200))
    result, code = lifecycle.unmint_all_minted()
    assert code == 200 and result["unminted_count"] == 0

    # no committed base -> the 503 guard propagates
    monkeypatch.setattr(lifecycle, "list_minted_variations", lambda: ({"error": "no base"}, 503))
    assert lifecycle.unmint_all_minted()[1] == 503


def test_committed_base_keys_reads_the_real_csv_and_never_falls_back_to_the_seed(tmp_path, monkeypatch):
    """#1: 'minted' = not in the CURRENT base file, and ONLY that. Reads a real CSV; an ABSENT base returns
    EMPTY (so callers 503) instead of silently falling back to the frozen pristine seed and over-removing."""
    import types
    from stone_pipeline import lifecycle
    from stone_pipeline.config import settings as settings_mod
    base = tmp_path / "variants_export_base.csv"
    base.write_text("Key,Name,Image,Aliases\nslab_granite_a_1,A,,\nblock_granite_a_2,A,,\n,blank,,\n",
                    encoding="utf-8")
    seed = tmp_path / "variants_export_base.seed.csv"
    seed.write_text("Key,Name\nslab_granite_SEEDONLY_9,Seed\n", encoding="utf-8")    # a seed that DIFFERS
    # _committed_base_keys imports SETTINGS function-locally, so patch the settings MODULE attribute it reads
    monkeypatch.setattr(settings_mod, "SETTINGS", types.SimpleNamespace(paths=types.SimpleNamespace(
        variants_export_base_csv=base, variants_export_base_seed_csv=seed)))
    assert lifecycle._committed_base_keys() == {"slab_granite_a_1", "block_granite_a_2"}   # blank Key dropped
    base.unlink()                                                                       # base absent post-roll
    assert lifecycle._committed_base_keys() == set()                                    # NOT the seed's keys


def test_unmint_against_a_real_ledger_tombstones_every_sibling_and_keeps_the_key_unexcluded(tmp_path, monkeypatch):
    """#4 seam test -- the REAL path: a real Ledger and the real sync.retire_variation / in-flight guard /
    live-products check (only the ledger file location is swapped). ONE Key of a 3-Key synced variety ->
    three kind='variation' tombstones (the removals pull deletes all three), the rows held 'retiring', the
    bystander untouched, the mint decision cleared, and the Key NOT in the retired-exclusion (resurfaces)."""
    from stone_pipeline import lifecycle
    from stone_pipeline.ledger import sync
    from stone_pipeline.ledger.db import Ledger, now_iso
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    decisions_store.set_variety_decision("Crystal White", "mint", seed_type="Granite")
    p, now = tmp_path / "dev.ledger", now_iso()

    def _var(ledger, key, name):
        ledger.upsert("variation", {"key": key, "branch": key.split("_")[0], "type": "Granite", "name": name,
                                    "aliases": "[]", "image_url": "", "image_sha256": None,
                                    "image_model": None, "volume": "", "medusa_id": "V", "in_full": 1,
                                    "payload_hash": "", "state": "synced", "first_seen": now,
                                    "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))

    sibs = ["slab_granite_crystal_white_aaaaaaaa-aaaa-5aaa-aaaa-aaaaaaaaaaaa",
            "block_granite_crystal_white_bbbbbbbb-bbbb-5bbb-bbbb-bbbbbbbbbbbb",
            "tile_granite_crystal_white_cccccccc-cccc-5ccc-cccc-cccccccccccc"]
    other = "slab_granite_absolute_black_dddddddd-dddd-5ddd-dddd-dddddddddddd"
    with Ledger.open(p, env="development") as ledger:
        for k in sibs:
            _var(ledger, k, "Crystal White")
        _var(ledger, other, "Absolute Black")

    def _real_op(name, work):                       # real ledger + real sync; only the path is swapped
        with Ledger.open(p, env="development") as ledger:
            return work(ledger, sync), 200
    monkeypatch.setattr(lifecycle, "_ledger_op", _real_op)

    result, code = lifecycle.unmint_variation(sibs[0])                              # ONE key in
    assert code == 200 and result["variety_count"] == 1
    assert sorted(result["unminted"]) == sorted(sibs)                              # all three siblings out
    with Ledger.open(p, env="development") as ledger:
        removed = {(r["external_id"], r["kind"])
                   for r in ledger.execute("SELECT external_id, kind FROM removed")}
        assert removed == {(k, "variation") for k in sibs}                         # tombstoned; bystander not
        for k in sibs:
            assert ledger.get("variation", "key", k)["state"] == "retiring"        # held until Medusa acks
        assert ledger.get("variation", "key", other)["state"] == "synced"          # bystander untouched
    assert decisions_store.confirm_map() == {}                                     # mint decision cleared
    assert store.load_retired() == set()                                           # NOT excluded -> resurfaces
