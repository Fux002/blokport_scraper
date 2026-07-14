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
