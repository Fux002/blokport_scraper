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
    # the real normalize + consolidate + collapse path that writes the base each run.
    from stone_pipeline.stages import emit_catalog
    emit_catalog.build()
    first = _FULL.read_bytes()
    emit_catalog.build()
    second = _FULL.read_bytes()
    assert first == second, "1_variants_full is not byte-identical on rebuild -- the seed is not a fixed point"


@pytest.mark.skipif(not _BASE.exists(), reason="no committed variants_export_base.csv seed")
def test_committed_base_is_already_a_fixed_point():
    # The committed base's SEED IDENTITY (Key/Name/Aliases/Volume) must equal what a rebuild projects back.
    # This is the ops guard `seed.verify` -- asserting it here keeps a future edit from committing a base
    # that would silently drift on the next cold start. verify() rebuilds HERMETICALLY (seed_only: no live
    # export / delta / config.db / S3), so it gives the same answer on CI and on an S3-authenticated dev
    # shell -- the old "seed FAILED" that only meant the ambient environment differed is gone.
    from stone_pipeline.reference import seed
    stats = seed.verify()
    assert stats["fixed_point"], f"committed base drifts on rebuild (first diff at row {stats['first_diff_row']})"
    # A (type, Name) variety may exist under ONLY ONE Key. Two Keys for the same stone+name is a
    # duplicate variety -- invisible to the fixed-point check (distinct Keys stay a stable fixed point),
    # so it is guarded explicitly here; it is the class that shipped duplicate products.
    assert stats["duplicate_varieties"] == 0, f"committed base has {stats['duplicate_varieties']} duplicate varieties"
    # Every Key must carry a real stone type. A type-less / mis-keyed variety (type slug 'alpine', minted
    # before the type-less-mint guard) is fixed-point-invisible too, and ships type-less to Medusa.
    assert stats["malformed_type_keys"] == 0, f"committed base has {stats['malformed_type_keys']} type-less/mis-keyed varieties"
    # A MIS-TYPED variety (name's stone-type word contradicts the Key's type, e.g. 'Agata White' keyed
    # semi_precious_stone) is a data error the pipeline deletes and the emit drops.
    assert stats["mistyped_variants"] == 0, f"committed base has {stats['mistyped_variants']} mistyped varieties"
    assert stats["clean"], "committed seed is not clean (fixed_point / duplicate / type-less / mistyped check failed)"


@pytest.mark.skipif(not _BASE.exists(), reason="no committed variants_export_base.csv seed")
def test_no_committed_variety_is_mangled_by_clean_variety_name():
    # clean_variety_name strips grade/code artifacts from a SCRAPED name so it matches the backbone (e.g.
    # 'Rosal C' -> 'Rosal', a leading 'Z' code dropped). Its SAFETY invariant: a real variety name must be a
    # FIXED POINT of the cleaning -- else a scraped copy of that variety would clean to something ELSE and
    # fail to match its own entry, or (for a trailing grade letter) silently collapse onto a DIFFERENT
    # variety. Enforce it on the committed base so a future base edit cannot add a variety clean_variety_name
    # would mangle. This is the guard the trailing-grade collapse relies on (it assumes no real variety ends
    # in a lone non-'I' letter); asserting the full fixed-point covers grade letters, number codes, and leads.
    from stone_pipeline.core.text import clean_variety_name
    from stone_pipeline.reference import seed
    names = sorted({r["Name"] for r in seed._rows(_BASE) if r.get("Name")})
    mangled = [(n, clean_variety_name(n)) for n in names if clean_variety_name(n) != n]
    assert not mangled, \
        f"{len(mangled)} committed varieties are NOT fixed points of clean_variety_name: {mangled[:10]}"


def test_malformed_type_keys_flags_type_less_keys():
    # deterministic (no S3, no seed): the guard flags a Key whose type slug is not a real stone type
    from stone_pipeline.reference import seed
    rows = [{"Key": "marble__carrara_x"},                     # real type -> ok
            {"Key": "semi_precious_stone__smoky_x"},          # real multi-word type -> ok
            {"Key": "alpine__luxe_x"},                        # 'alpine' is not a stone type -> flagged
            {"Key": "ice__burg_x"}]                           # 'ice' is not a stone type -> flagged
    bad = seed._malformed_type_keys(rows)
    assert bad == ["alpine__luxe_x", "ice__burg_x"]           # sorted, only the type-less ones


def test_alias_normalization_is_idempotent():
    # The base-drift source: comma-joined blobs, a (bracket) alias, and mixed case all normalize on the
    # first pass; a second pass must be a no-op, or the base never reaches a fixed point.
    name = "Grey Emperador"
    raw = ["Gray Emperador, Emperador Grey", "(Silver Emperador)", "GREY EMPEROR", "Grey Emperador"]
    from functools import partial
    from stone_pipeline.reference.loaders import field_kinds as fk
    clean = partial(fk.display, fk.VARIATION_ALIAS_VALUE)
    once, _ = clean_alias_list(name, raw, clean)
    twice, _ = clean_alias_list(name, once, clean)
    assert once == twice, f"clean_alias_list is not idempotent: {once} -> {twice}"
    # a second pass over an already-clean list changes nothing
    assert clean_alias_list(name, twice, clean) == (twice, [])


def test_title_case_is_idempotent():
    for s in ("CARRARA", "giallo di siena", "Nero Marquina", "verde ubatuba"):
        assert title_case(title_case(s)) == title_case(s)
