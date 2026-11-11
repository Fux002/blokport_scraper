"""The demand-driven texture upgrade: legacy icons are re-made ONCE, only for a pair a product links to, and the
already-on-S3 prune must NOT drop them (their icon exists by definition). Per (Key, category) pair (spec rev 8
section 4): the pair ids are `{Key}/{category}`."""

import json
import types

from stone_pipeline import prepare_variant_images as pvi
from stone_pipeline.stages import image_prompts
from stone_pipeline.tests._marker_s3 import MarkerS3

K = "marble__carrara_00000000-1111-5222-8333-944455556666"
SLAB, BLOCK, TILE = f"{K}/slab", f"{K}/block", f"{K}/tile"


def _backed(monkeypatch, pairs):
    monkeypatch.setattr(image_prompts, "product_backed_pairs", lambda: set(pairs))


def test_upgrade_is_demand_driven_and_needs_an_existing_icon(monkeypatch):
    _backed(monkeypatch, {(K, "slab"), (K, "block"), (K, "tile")})
    got = image_prompts.upgrade_candidates(set(), cap=10, icons={SLAB, TILE})   # block has no icon: a MINT
    assert got == [SLAB, TILE]                                                   # deterministic order


def test_upgrade_is_once_only_and_capped(monkeypatch):
    _backed(monkeypatch, {(K, "slab"), (K, "tile")})
    icons = {SLAB, TILE}
    assert image_prompts.upgrade_candidates({SLAB}, cap=10, icons=icons) == [TILE]   # the marker skips it
    assert image_prompts.upgrade_candidates(set(), cap=1, icons=icons) == [SLAB]     # the cap applies
    assert image_prompts.upgrade_candidates(set(), cap=0, icons=icons) == []         # 0 == disabled


def test_prune_keeps_upgrades_but_still_drops_mints(tmp_path, monkeypatch):
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([
        {"output_name": SLAB, "prompt": "p", "mode": image_prompts.UPGRADE_MODE},
        {"output_name": BLOCK, "prompt": "p"},                                  # a mint already done
        {"output_name": TILE, "prompt": "p"},                                   # a new mint
    ]), encoding="utf-8")
    monkeypatch.setattr(pvi, "_existing_icons", lambda: {SLAB, BLOCK})
    kept = pvi._prune_already_on_s3(q, [SLAB, BLOCK, TILE])
    assert kept == [SLAB, TILE]                                                 # the upgrade survives
    names = {i["output_name"] for i in json.loads(q.read_text(encoding="utf-8"))}
    assert names == {SLAB, TILE}                                                # the rewritten queue matches


def test_a_fresh_mint_is_marked_so_the_drip_never_redoes_it(monkeypatch):
    """The cold-start loop: mint -> icon made by the BEST model -> marked. If a mint were left unmarked, the
    upgrade drip would later regenerate (and re-charge for) an already-best icon."""
    from stone_pipeline import refresh_images

    s3 = MarkerS3()                                         # a first run: no marker yet
    monkeypatch.setattr(refresh_images, "_s3_client", lambda: s3)
    monkeypatch.setattr(refresh_images, "SETTINGS", types.SimpleNamespace(
        s3=types.SimpleNamespace(bucket="b", dry_run=False), images=types.SimpleNamespace(marker_write_attempts=3)))

    assert refresh_images.mark_best_model([SLAB]) is True
    assert s3.keys() == {SLAB}
    refresh_images.mark_best_model([TILE])                  # merges, never truncates
    assert s3.keys() == {SLAB, TILE}
    assert refresh_images.mark_best_model([]) is False      # nothing to do, no write

    # a marked mint is no longer an upgrade candidate
    _backed(monkeypatch, {(K, "slab")})
    assert image_prompts.upgrade_candidates(s3.keys(), cap=10, icons={SLAB}) == []
