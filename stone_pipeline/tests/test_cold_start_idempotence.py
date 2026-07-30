"""Cold-start idempotence: the committed seed must be a FIXED POINT so repeated cold starts reproduce
byte-identically. Two levels:

  * end-to-end: rebuilding 1_variants_full from the committed base twice is byte-identical (the property
    `stone_pipeline.reference.seed.verify` checks in ops). Skipped if the committed seed is absent.
  * unit: the alias normalization that runs every build is idempotent -- it is the ONE transform that
    made a hand-cleaned base drift on the first rebuild, so it must converge in a single pass.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core.text import clean_alias_list, title_case

_BASE = SETTINGS.paths.variants_export_base_csv
_FULL = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"


@pytest.mark.skipif(not _BASE.exists(), reason="no committed variants_export_base.csv seed to rebuild from")
def test_emit_build_from_seed_is_idempotent():
    # Build the full variant list from the committed base twice; the outputs must be byte-identical.
    # emit_catalog reads only the seed + generated artifacts (absent in CI -> empty), so this exercises
    # the real normalize + union-fill + consolidate path that writes the base each run.
    from stone_pipeline.stages import emit_catalog
    emit_catalog.build()
    first = _FULL.read_bytes()
    emit_catalog.build()
    second = _FULL.read_bytes()
    assert first == second, "1_variants_full is not byte-identical on rebuild -- the seed is not a fixed point"


@pytest.mark.skipif(not _BASE.exists(), reason="no committed variants_export_base.csv seed")
def test_committed_base_is_already_a_fixed_point():
    # The committed base must equal what a rebuild projects back (base == full projection). This is the
    # ops guard `seed.verify` -- asserting it here keeps a future edit from committing a base that would
    # silently drift on the next cold start.
    from stone_pipeline.reference import seed
    stats = seed.verify()
    assert stats["fixed_point"], f"committed base drifts on rebuild (first diff at row {stats['first_diff_row']})"
    # A (branch,type,Name) variety may exist under ONLY ONE Key. Two Keys for the same stone+name is a
    # duplicate variety -- invisible to the fixed-point check (distinct Keys stay a stable fixed point),
    # so it is guarded explicitly here; it is the class that shipped duplicate products.
    assert stats["duplicate_varieties"] == 0, f"committed base has {stats['duplicate_varieties']} duplicate varieties"
    # Every Key must carry a real stone type. A type-less / mis-keyed variety (slab_alpine_luxe, minted
    # before the type-less-mint guard) is fixed-point-invisible too, and ships type-less to Medusa.
    assert stats["malformed_type_keys"] == 0, f"committed base has {stats['malformed_type_keys']} type-less/mis-keyed varieties"
    assert stats["clean"], "committed seed is not clean (fixed_point / duplicate / type-less check failed)"


def test_malformed_type_keys_flags_type_less_keys():
    # deterministic (no S3, no seed): the guard flags a Key whose type slug is not a real stone type
    from stone_pipeline.reference import seed
    rows = [{"Key": "slab_marble_carrara_x"},                 # real type -> ok
            {"Key": "slab_semi_precious_stone_smoky_x"},      # real multi-word type -> ok
            {"Key": "slab_alpine_luxe_x"},                    # 'alpine' is not a stone type -> flagged
            {"Key": "block_ice_burg_x"}]                      # 'ice' is not a stone type -> flagged
    bad = seed._malformed_type_keys(rows)
    assert bad == ["block_ice_burg_x", "slab_alpine_luxe_x"]  # sorted, only the type-less ones


@pytest.mark.skipif(not _BASE.exists(), reason="no committed seed")
def test_every_category_carries_the_full_variety_union():
    """A variety belongs to the stone, not a format, so every active category must carry the FULL UNION of
    varieties across all categories -- no slab without its block/tile, and a block-only variety must also
    appear in slab and tile (no category is a master). The emit's union gap-fill establishes this and
    `seed.verify` guards it; assert it on the committed base so a base edit or an emit regression that
    breaks category uniformity is caught. Supersedes the old slab-master mirror / backbone-orphan checks."""
    from stone_pipeline.reference import seed
    base = seed._rows(_BASE)
    assert seed._non_uniform_categories(base) == [], \
        "a category does not carry the full variety union -- the seed is not category-uniform"


def test_alias_normalization_is_idempotent():
    # The base-drift source: comma-joined blobs, a (bracket) alias, and mixed case all normalize on the
    # first pass; a second pass must be a no-op, or the base never reaches a fixed point.
    name = "Grey Emperador"
    raw = ["Gray Emperador, Emperador Grey", "(Silver Emperador)", "GREY EMPEROR", "Grey Emperador"]
    once = clean_alias_list(name, raw)
    twice = clean_alias_list(name, once)
    assert once == twice, f"clean_alias_list is not idempotent: {once} -> {twice}"
    # a second pass over an already-clean list changes nothing
    assert clean_alias_list(name, twice) == twice


def test_title_case_is_idempotent():
    for s in ("CARRARA", "giallo di siena", "Nero Marquina", "verde ubatuba"):
        assert title_case(title_case(s)) == title_case(s)
