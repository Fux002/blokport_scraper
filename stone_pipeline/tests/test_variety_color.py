"""Variety colour: the perceptual classifier and product-image seeding.

Colour comes from a variety's REAL product image (the de-watermarked scraped photo), not the generated
variant icon (which can be a stale/placeholder render). These tests lock: (1) the classifier names a stone
by its saturated accent, not its washed-out median; (2) fill_colors seeds a variety from its product photo,
re-derives ONLY when the photo changes (sha), and floors to the pack default when there is no photo.
"""

from __future__ import annotations

import io
import json

import numpy as np
import pytest

from stone_pipeline.stages import variety_color

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _stone(rgb, n=8000):
    """An RGBA pixel block of a stone colour with light noise (a bg-removed texture)."""
    arr = np.clip(np.tile([*rgb, 255], (n, 1)) + np.random.randint(-12, 12, (n, 4)), 0, 255)
    return arr.astype("uint8").reshape(1, -1, 4)


def _png(rgb):
    b = io.BytesIO()
    Image.fromarray(_stone(rgb)[0].reshape(1, -1, 4), "RGBA").save(b, "PNG")
    return b.getvalue()


# --- classifier: saturated accent survives light veining ----------------------
def test_dominant_color_names_the_saturated_accent_not_the_median():
    np.random.seed(0)
    # 65% saturated green + 35% cream veining: a per-channel median lands warm (Beige/Cream); the perceptual
    # classifier must still name it Green (the veined-quartzite failure that mislabelled Sauipe).
    green = np.tile([70, 150, 80, 255], (13000, 1)) + np.random.randint(-15, 15, (13000, 4))
    cream = np.tile([225, 220, 200, 255], (7000, 1)) + np.random.randint(-10, 10, (7000, 4))
    img = np.clip(np.vstack([green, cream]), 0, 255).reshape(1, -1, 4)
    assert variety_color.dominant_color(img) == "Green"


def test_dominant_color_keeps_the_neutral_ramp_for_a_genuinely_neutral_stone():
    np.random.seed(1)
    grey = _stone([130, 130, 132], 20000)
    white = _stone([236, 234, 230], 20000)
    assert variety_color.dominant_color(grey) == "Grey"
    assert variety_color.dominant_color(white) in {"White", "Cream"}


def test_dominant_color_none_when_too_few_opaque_pixels():
    arr = np.zeros((1, 10, 4), dtype="uint8")   # all transparent
    assert variety_color.dominant_color(arr) is None


# --- fill_colors: seed from the product photo, re-derive only on change --------
def _fake_fetch(store):
    return lambda url: store.get(url.rsplit("/", 1)[-1].split(".")[0])


def _url(sha):
    return f"https://x.s3.eu-west-1.amazonaws.com/dev/products/improved/varsha/{sha}.jpg"


def test_fill_colors_seeds_from_product_image_and_is_not_sticky(tmp_path):
    np.random.seed(2)
    green, warm = "a" * 64, "b" * 64
    store = {green: _png([70, 150, 80]), warm: _png([220, 215, 195])}
    bb = tmp_path / "backbone_additions.json"
    # Sauipe currently wrongly Beige (from the old icon path), with no recorded photo sha
    bb.write_text(json.dumps([
        {"key": "slab_quartzite_sauipe_x", "variant": "Sauipe", "stone_type": "Quartzite",
         "color": ["Beige", "Cream", "Gold"]},
        {"key": "slab_granite_none_y", "variant": "NoPhoto", "stone_type": "Granite", "color": []},
    ]), encoding="utf-8")

    # run 1: recompute Sauipe from its REAL green photo; the photoless variety floors to Natural
    pi = {"slab_quartzite_sauipe_x": _url(green)}
    variety_color.fill_colors(backbone_paths=[bb], product_images=pi, fetch=_fake_fetch(store))
    out = {p["key"]: p.get("color") for p in json.loads(bb.read_text())}
    assert out["slab_quartzite_sauipe_x"] == ["Green"]        # Beige -> Green from the photo
    assert out["slab_granite_none_y"] == ["Natural"]          # no photo -> pack floor

    # run 2: same photo -> NOT re-derived (sha unchanged) -- idempotent, no re-download
    st2 = variety_color.fill_colors(backbone_paths=[bb], product_images=pi, fetch=_fake_fetch(store))
    assert st2["from_image"] == 0

    # run 3: the photo CHANGED (new sha) -> re-derive
    st3 = variety_color.fill_colors(backbone_paths=[bb],
                                    product_images={"slab_quartzite_sauipe_x": _url(warm)},
                                    fetch=_fake_fetch(store))
    assert st3["from_image"] == 1
    assert json.loads(bb.read_text())[0]["color"] != ["Green"]   # updated off the changed photo
