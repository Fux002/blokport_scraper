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
    # the real normalize + mirror + consolidate path that writes the base each run.
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
def test_no_orphan_mirror_backbone_rows():
    """Every mirror-category (tile) post's (stone_type, variant) must have a matching source (slab) post.
    emit's mirror join is by (stone_type, variant), so a tile with no source slab is silently dropped from
    the mirror. A seed edit that removes a slab but leaves its tile creates exactly that orphan -- a real
    regression once caused by a dedup keeping a base-only slab key and deleting the backbone-backed one."""
    import json
    from stone_pipeline.config.settings import CATEGORIES, category
    from stone_pipeline.stages.emit_catalog import _posts_of
    for cat in CATEGORIES:
        if not cat.mirror_of:
            continue
        src_path, mir_path = category(cat.mirror_of).backbone_path, cat.backbone_path
        if not (src_path.exists() and mir_path.exists()):
            continue
        src = {(p.get("stone_type"), p.get("variant"))
               for p in _posts_of(json.loads(src_path.read_text(encoding="utf-8-sig")))}
        orphans = [(p.get("stone_type"), p.get("variant"))
                   for p in _posts_of(json.loads(mir_path.read_text(encoding="utf-8-sig")))
                   if (p.get("stone_type"), p.get("variant")) not in src]
        assert not orphans, f"{cat.name}: {len(orphans)} orphan mirror rows with no source slab: {orphans[:5]}"


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
