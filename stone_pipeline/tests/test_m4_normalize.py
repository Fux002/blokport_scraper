"""M4 DoD: known synonyms resolve, unknowns flag, no guess reaches output.
Plus Stage 2 surrogate minting and dedup, and the matching-engine negatives
(token_set false positives like Crystal Frost -> Crystal must be rejected).
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.matching.engine import VocabResolver
from stone_pipeline.reference import loaders
from stone_pipeline.stages import keys_dedupe, normalize


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


# --- Stage 3 normalize --------------------------------------------------------
def test_synonym_resolves_quality(ref):
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", raw_quality="First")
    normalize.normalize_row(row, resolvers, ref)
    assert row.quality_name == "A"
    assert row.quality_id is not None
    assert row.quality_method == "synonym"


def test_exact_resolves_finish(ref):
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", raw_finish="polished")
    normalize.normalize_row(row, resolvers, ref)
    assert row.finish_name == "Polished"
    assert row.finish_id is not None


def test_synonym_none_is_clean_not_error(ref):
    # finish 'Other' -> none: resolves to no id, but is not an unresolved error.
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="marenostone", raw_finish="Other")
    normalize.normalize_row(row, resolvers, ref)
    assert row.finish_id is None
    assert row.finish_method == "synonym_none"
    assert not any(f.code == FlagCode.attr_unresolved for f in row.review_flags)


def test_unknown_value_flags_and_does_not_guess(ref):
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="x", raw_color="Chartreuse Sparkle")
    normalize.normalize_row(row, resolvers, ref)
    assert row.color_id is None  # no guess reaches output
    assert any(f.code == FlagCode.attr_unresolved and f.field == "color" for f in row.review_flags)


def test_multi_value_takes_first_and_flags(ref):
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", raw_color="Black | White")   # genuine separator
    normalize.normalize_row(row, resolvers, ref)
    assert row.color_name == "Black"
    assert any(f.code == FlagCode.multi_value for f in row.review_flags)


def test_compound_and_value_ships_via_first_conjunct(ref):
    # ' and ' is NOT a primary separator (so a single descriptor that resolves WHOLE stays whole), but
    # 'Black and White' isn't a vocab colour, so it falls back to the first resolvable conjunct ('Black')
    # + a multi_value flag -- the product SHIPS instead of being hard-rejected for a null colour id.
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", raw_color="Black and White")
    normalize.normalize_row(row, resolvers, ref)
    assert row.color_name == "Black"
    assert any(f.code == FlagCode.multi_value for f in row.review_flags)


def test_vocab_fuzzy_floor_rejects_low_score():
    resolver = VocabResolver(
        vocab="finish",
        canonical_values=["Polished", "Honed", "Leathered"],
        synonyms={},
        fuzzy_floor=90.0,
    )
    # close typo accepts
    assert resolver.resolve("Polishd").value == "Polished"
    # nonsense rejects to None (no guess)
    assert resolver.resolve("Zxqw").value is None


def test_name_over_tag_uses_canonical_casing(ref):
    # 'Z B CREAM QUARTZITE' (varsha, all-caps) mis-tagged Onyx: the explicit name word
    # wins, but the emitted type_name must be the canonical 'Quartzite', not the raw
    # uppercase token leaked from the variety name.
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="varsha", raw_name="Cream QUARTZITE", raw_type="Onyx")
    normalize.normalize_row(row, resolvers, ref)
    assert row.type_name == "Quartzite"
    assert row.type_method == "name_explicit"
    assert row.type_id is not None


# --- Stage 2 keys / dedup -----------------------------------------------------
def _row(site, key, name="Stone", color="Black"):
    return CanonicalRow(src_site=site, src_natural_key=key, raw_name=name, raw_color=color)


def test_natural_key_becomes_surrogate():
    rows = [_row("polonine", "620"), _row("polonine", "621")]
    result = keys_dedupe.run(rows)
    assert result.rows[0].surrogate_key == "620"
    assert result.minted == 0


def test_blank_key_is_minted_deterministically():
    a = CanonicalRow(src_site="marenostone", src_natural_key="", raw_name="Cream Tile", src_url="http://x/1")
    result = keys_dedupe.run([a])
    assert result.minted == 1
    assert result.rows[0].surrogate_key.startswith("mint_")
    assert any(f.code == FlagCode.surrogate_minted for f in result.rows[0].review_flags)


def test_exact_dedup_keeps_first():
    rows = [_row("polonine", "620"), _row("polonine", "620"), _row("polonine", "621")]
    result = keys_dedupe.run(rows)
    assert len(result.rows) == 2
    assert result.dropped_exact == 1


def test_near_duplicate_flag_not_merge():
    rows = [
        _row("polonine", "620", name="Alpine", color="White"),
        _row("polonine", "621", name="Alpine", color="White"),
    ]
    result = keys_dedupe.run(rows)
    assert len(result.rows) == 2  # not merged
    assert result.near_duplicates == 1
