"""The demand-driven texture upgrade: legacy textures are re-made ONCE, only when a product links to
them, and the already-on-S3 prune must NOT drop them (their PNG exists by definition)."""

import json
import types

from stone_pipeline.stages import image_prompts
from stone_pipeline import prepare_variant_images as pvi
from stone_pipeline.tests._marker_s3 import MarkerS3


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


def test_a_fresh_mint_is_marked_so_the_drip_never_redoes_it(monkeypatch):
    """The cold-start loop: mint -> texture made by the BEST model -> marked. If a mint were left
    unmarked, the upgrade drip would later regenerate (and re-charge for) an already-best texture."""
    from stone_pipeline import refresh_images

    s3 = MarkerS3()                                         # a first run: no marker yet
    monkeypatch.setattr(refresh_images, "_s3_client", lambda: s3)
    monkeypatch.setattr(refresh_images, "SETTINGS", types.SimpleNamespace(
        s3=types.SimpleNamespace(bucket="b", dry_run=False), images=types.SimpleNamespace(marker_write_attempts=3)))

    assert refresh_images.mark_best_model(["slab_new"]) is True
    assert s3.keys() == {"slab_new"}
    refresh_images.mark_best_model(["slab_other"])          # merges, never truncates
    assert s3.keys() == {"slab_new", "slab_other"}
    assert refresh_images.mark_best_model([]) is False      # nothing to do, no write

    # a marked mint is no longer an upgrade candidate
    _stub(monkeypatch, {"slab_new"}, {"slab_new": {"Name": "N", "Image": "u"}})
    assert image_prompts.upgrade_candidates(s3.keys(), cap=10) == []
