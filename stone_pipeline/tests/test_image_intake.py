"""Image intake: filename -> Key matching (stem-based, robust to rb_<id>)."""

from __future__ import annotations

from stone_pipeline.stages import image_intake as ii


def test_key_and_file_stems_align():
    key = "slab_agate_agata_black_043e2e2e-83af-4986-8a1a-fddedd69e70b"
    assert ii.key_stem(key) == "slab_agate_agata_black"
    # old backbone name and the export's -<uuid> form both reduce to the Key stem
    assert ii.file_stem("slab_rb_318_agate_agata_black.png") == "slab_agate_agata_black"
    assert ii.file_stem(
        "block_rb_5_marble_bardiglio_nuvolato-3e9d07f5-7e8e-4503-834c-1b2f071ef219.png"
    ) == "block_marble_bardiglio_nuvolato"
    assert ii.file_stem("slab_rb_9_marble_x_crop_555x300.png") == "slab_marble_x"


def test_match_image_exact_stem_ambiguous_none():
    by_stem = {"slab_agate_agata_black": {"slab_agate_agata_black_abc"},
               "slab_dup": {"k1", "k2"}}
    by_base = {"slab_rb_318_agate_agata_black.png": "slab_agate_agata_black_abc"}
    # exact basename wins
    assert ii.match_image("slab_rb_318_agate_agata_black.png", by_stem, by_base) == \
        ("slab_agate_agata_black_abc", "exact")
    # different rb id, same stem -> stem match
    assert ii.match_image("slab_rb_999_agate_agata_black.png", by_stem, by_base) == \
        ("slab_agate_agata_black_abc", "stem")
    # duplicate variety name -> ambiguous, not guessed
    key, method = ii.match_image("slab_rb_1_dup.png", by_stem, by_base)
    assert key is None and method.startswith("ambiguous")
    # nothing matches
    assert ii.match_image("slab_rb_1_unknown_xyz.png", by_stem, by_base) == (None, "no_variant")
