"""The demand-driven texture upgrade: legacy textures are re-made ONCE, only when a product links to
them, and the already-on-S3 prune must NOT drop them (their PNG exists by definition)."""

import json

from stone_pipeline.stages import image_prompts
from stone_pipeline import prepare_variant_images as pvi


def _stub(monkeypatch, backed, variants):
    monkeypatch.setattr(image_prompts, "product_backed_keys", lambda: set(backed))
    monkeypatch.setattr(image_prompts, "_variants", lambda: variants)
    monkeypatch.setattr(image_prompts, "_backbone_types", lambda: {k: "Marble" for k in variants})


def test_upgrade_is_demand_driven_and_needs_an_existing_image(monkeypatch):
    _stub(monkeypatch, {"slab_a", "slab_b", "slab_c"}, {
        "slab_a": {"Name": "A", "Image": "https://x/slab_a.png"},   # backed + imaged -> upgrade
        "slab_b": {"Name": "B", "Image": ""},                       # imageless -> a MINT, not an upgrade
        "slab_c": {"Name": "C", "Image": "https://x/slab_c.png"},
    })
    got = image_prompts.upgrade_candidates(set(), cap=10)
    assert got == ["slab_a", "slab_c"]                              # slab_b excluded, deterministic order


def test_upgrade_is_once_only_and_capped(monkeypatch):
    _stub(monkeypatch, {"slab_a", "slab_c"}, {
        "slab_a": {"Name": "A", "Image": "u"}, "slab_c": {"Name": "C", "Image": "u"}})
    assert image_prompts.upgrade_candidates({"slab_a"}, cap=10) == ["slab_c"]   # marker skips it
    assert image_prompts.upgrade_candidates(set(), cap=1) == ["slab_a"]         # cap applies
    assert image_prompts.upgrade_candidates(set(), cap=0) == []                 # 0 == disabled


def test_prune_keeps_upgrades_but_still_drops_mints(monkeypatch, tmp_path):
    """The regression that would silently make upgrades a no-op: an upgrade's PNG is already on S3."""
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([
        {"output_name": "slab_up", "prompt": "p", "mode": image_prompts.UPGRADE_MODE},
        {"output_name": "slab_mint_done", "prompt": "p"},
        {"output_name": "slab_mint_new", "prompt": "p"},
    ]))
    monkeypatch.setattr("stone_pipeline.stages.emit_catalog._s3_variation_keys",
                        lambda: {"slab_up", "slab_mint_done"})
    kept = pvi._prune_already_on_s3(q, ["slab_up", "slab_mint_done", "slab_mint_new"])
    assert kept == ["slab_up", "slab_mint_new"]        # upgrade survives, done-mint pruned
    names = {i["output_name"] for i in json.loads(q.read_text())}
    assert names == {"slab_up", "slab_mint_new"}       # rewritten queue matches


def test_ledger_records_the_generator_and_never_re_serves_for_it(tmp_path, monkeypatch):
    """image_model is provenance, not content: it must be stored, refreshed on re-run, and must NOT
    change payload_hash (recording who made a texture cannot re-sync the variant to Medusa)."""
    import csv as _csv
    from stone_pipeline.ledger import populate as pop
    from stone_pipeline.ledger.db import Ledger

    cs = tmp_path / "catalog_source"; cs.mkdir()
    with (cs / "image_model.csv").open("w", newline="", encoding="utf-8") as h:
        w = _csv.writer(h); w.writerow(["Key", "model", "generated"])
        w.writerow(["slab_a", "flux-2-max", "2026-09-07"])
    audit = cs / "image_model.csv"
    assert pop._image_models(audit) == {"slab_a": "flux-2-max"}
    assert pop._image_models(tmp_path / "nope.csv") == {}      # absent audit -> empty, never a crash
    monkeypatch.setattr(pop, "_image_models", lambda *a, **k: {"slab_a": "flux-2-max"})

    full = tmp_path / "1_variants_full.csv"
    with full.open("w", newline="", encoding="utf-8") as h:
        w = _csv.writer(h); w.writerow(list(pop._VARIANTS_FULL_COLS))
        w.writerow(["slab_a", "A", "https://x/slab_a.png", "", "0.001"])
        w.writerow(["slab_b", "B", "https://x/slab_b.png", "", "0.001"])

    with Ledger.open(tmp_path / "t.ledger", env="development") as led:
        pop.populate_variations_full(led, full)
        rows = {r[0]: r[1:] for r in led.execute(
            "SELECT key, image_model, payload_hash FROM variation").fetchall()}
        assert rows["slab_a"][0] == "flux-2-max"   # recorded from the audit
        assert rows["slab_b"][0] is None           # absent from the audit -> no provenance, not a crash
        ph_before = rows["slab_a"][1]

        pop.populate_variations_full(led, full)    # idempotent re-run
        after = {r[0]: r[1:] for r in led.execute(
            "SELECT key, image_model, payload_hash FROM variation").fetchall()}
        assert after["slab_a"][0] == "flux-2-max"
        assert after["slab_a"][1] == ph_before     # provenance never re-serves the variant


def test_a_fresh_mint_is_marked_so_the_drip_never_redoes_it(monkeypatch):
    """The cold-start loop: mint -> texture made by the BEST model -> marked. If a mint were left
    unmarked, the upgrade drip would later regenerate (and re-charge for) an already-best texture."""
    from stone_pipeline import refresh_images

    marker: set[str] = set()
    monkeypatch.setattr(refresh_images, "_s3_client", lambda: object())
    monkeypatch.setattr(refresh_images, "load_refreshed", lambda client=None: set(marker))
    monkeypatch.setattr(refresh_images, "save_refreshed",
                        lambda keys, client=None: (marker.update(keys), True)[1])

    assert refresh_images.mark_best_model(["slab_new"]) is True
    assert marker == {"slab_new"}
    refresh_images.mark_best_model(["slab_other"])          # merges, never truncates
    assert marker == {"slab_new", "slab_other"}
    assert refresh_images.mark_best_model([]) is False      # nothing to do, no write

    # a marked mint is no longer an upgrade candidate
    _stub(monkeypatch, {"slab_new"}, {"slab_new": {"Name": "N", "Image": "u"}})
    assert image_prompts.upgrade_candidates(marker, cap=10) == []
