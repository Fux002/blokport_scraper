"""Tile images mirror their slab's image.

Tiles are mirror COMBINATIONS (the same stone as the slab) with no product or texture of their own, so a
tile's {Key}.png is never generated -- the export then blanks it (an image link must never 404). This left
~12k tiles imageless. mirror_slab_images_to_tiles copies each slab's image to its tile Key so the tile
advertises the same stone's photo. These tests lock the join, the skip conditions, and idempotency.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from stone_pipeline import catalog


class _Cat:
    def __init__(self, name, path, mirror_of=None, active=True):
        self.name, self.backbone_path, self.mirror_of, self.active = name, path, mirror_of, active


def _backbones(tmp_path):
    slab = tmp_path / "slab.json"
    tile = tmp_path / "tile.json"
    slab.write_text(json.dumps([
        {"key": "slab_marble_arabescato_ABC", "stone_type": "Marble", "variant": "Arabescato"},
        {"key": "slab_marble_carrara_DEF", "stone_type": "Marble", "variant": "Carrara"},
    ]), encoding="utf-8")
    tile.write_text(json.dumps([
        {"key": "tile_marble_arabescato_XYZ", "stone_type": "Marble", "variant": "Arabescato"},  # copy
        {"key": "tile_marble_carrara_UVW", "stone_type": "Marble", "variant": "Carrara"},         # slab not ready
        {"key": "tile_marble_ghost_QQQ", "stone_type": "Marble", "variant": "Ghost"},             # no slab
    ]), encoding="utf-8")
    return _Cat("slab", slab), _Cat("tile", tile, mirror_of="slab", active=True)


def test_mirror_fills_syncs_and_is_idempotent(tmp_path):
    slab, tile = _backbones(tmp_path)
    # only the Arabescato SLAB has an image (etag "A"); no tiles do; Carrara slab not ready; Ghost has no slab
    etags = {"slab_marble_arabescato_ABC": "A"}
    copies = []

    def copy(old, new):
        copies.append((old, new))
        return True

    with patch("stone_pipeline.config.settings.CATEGORIES", [slab, tile]), \
         patch("stone_pipeline.config.settings.category", lambda n: {"slab": slab, "tile": tile}[n]):
        # 1. FILL: copies only the Arabescato tile (Carrara slab not ready, Ghost has no slab sibling)
        n = catalog.mirror_slab_images_to_tiles(etags=etags, copier=copy)
        assert n == 1 and copies == [("slab_marble_arabescato_ABC", "tile_marble_arabescato_XYZ")]
        assert etags["tile_marble_arabescato_XYZ"] == "A"          # recorded in sync (won't re-copy)

        # 2. IDEMPOTENT: tile in sync with its slab -> nothing copied
        copies.clear()
        assert catalog.mirror_slab_images_to_tiles(etags=etags, copier=copy) == 0 and copies == []

        # 3. SLAB REGENERATED (etag changes) -> the tile re-syncs to the new image
        etags["slab_marble_arabescato_ABC"] = "B"
        copies.clear()
        n3 = catalog.mirror_slab_images_to_tiles(etags=etags, copier=copy)
        assert n3 == 1 and copies == [("slab_marble_arabescato_ABC", "tile_marble_arabescato_XYZ")]
        assert etags["tile_marble_arabescato_XYZ"] == "B"


def test_mirror_noop_when_s3_unreachable(tmp_path):
    slab, tile = _backbones(tmp_path)
    with patch("stone_pipeline.config.settings.CATEGORIES", [slab, tile]), \
         patch("stone_pipeline.config.settings.category", lambda n: {"slab": slab, "tile": tile}[n]):
        # no etags/copier available (CI/sandbox: S3 unreachable) -> clean no-op, never crashes
        assert catalog.mirror_slab_images_to_tiles(etags=None, copier=None) == 0
