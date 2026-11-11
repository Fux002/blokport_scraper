"""Image intake: filename -> Key matching (stem-based, robust to rb_<id>)."""

from __future__ import annotations

from stone_pipeline.stages import image_intake as ii


_K = "agate__agata_black_b7458ad2-8c77-5072-8c48-706034ce1833"


def test_key_and_file_stems_align():
    # the phase 1 Key's stem folds its type separator; an old inbox name in its category's folder reduces to it
    assert ii.key_stem(_K) == "agate_agata_black"
    assert ii.file_stem("slab_rb_318_agate_agata_black.png", "slab") == "agate_agata_black"
    assert ii.file_stem(
        "block_rb_5_marble_bardiglio_nuvolato-3e9d07f5-7e8e-4503-834c-1b2f071ef219.png", "block"
    ) == "marble_bardiglio_nuvolato"
    assert ii.file_stem("slab_rb_9_marble_x_crop_555x300.png", "slab") == "marble_x"


def test_match_image_exact_stem_ambiguous_none():
    by_stem = {"agate_agata_black": {_K}, "marble_dup": {"k1", "k2"}}
    by_key = {_K.lower(): _K}
    # the Key itself as the file name wins
    assert ii.match_image(f"{_K}.png", "slab", by_stem, by_key) == (_K, "exact")
    # an old name in its category's folder -> stem match
    assert ii.match_image("slab_rb_999_agate_agata_black.png", "slab", by_stem, by_key) == (_K, "stem")
    # a duplicate variety name -> ambiguous, not guessed
    key, method = ii.match_image("slab_rb_1_marble_dup.png", "slab", by_stem, by_key)
    assert key is None and method.startswith("ambiguous")
    # nothing matches
    assert ii.match_image("slab_rb_1_unknown_xyz.png", "slab", by_stem, by_key) == (None, "no_variant")
