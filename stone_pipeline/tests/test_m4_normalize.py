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


def test_extracted_portuguese_colour_resolves_end_to_end(ref):
    # the WHOLE colour path in one: extract_color recovers the colour from a zucchi Portuguese name, and
    # the resolver then resolves that raw value to the canonical colour + id. Extractor and resolver share
    # the one vocabulary, so what the extractor recovers is exactly what Stage 3 resolves.
    from stone_pipeline.adapters.tokens import extract_color
    resolvers = normalize.AttributeResolvers.build(ref)
    raw_color = extract_color("CIN Granito Preto Rio Negro 2cm")   # zucchi has no colour column
    assert raw_color == "Black"
    row = CanonicalRow(src_site="zucchi", raw_color=raw_color)
    normalize.normalize_row(row, resolvers, ref)
    assert row.color_name == "Black" and row.color_id is not None


def test_last_resort_finish_and_quality_defaults(ref):
    # an unresolvable finish/quality is not dropped: a configured, flagged last-resort default fills it
    # (slab finish -> Polished, tile -> Honed, block -> Raw; quality -> A) so the product still ships.
    resolvers = normalize.AttributeResolvers.build(ref)
    slab = CanonicalRow(src_site="polonine", raw_format="Slab", raw_finish="", raw_quality="")
    normalize.normalize_row(slab, resolvers, ref)
    assert slab.finish_name == "Polished" and slab.finish_id is not None
    assert slab.finish_method == "last_resort_default"
    assert slab.quality_name == "A" and slab.quality_id is not None and slab.quality_method == "last_resort_default"
    assert sum(1 for f in slab.review_flags if f.code == FlagCode.attr_last_resort) == 2
    tile = CanonicalRow(src_site="x", raw_format="Tile", raw_finish="", raw_quality="A")
    normalize.normalize_row(tile, resolvers, ref)
    assert tile.finish_name == "Honed"                 # tile default, not slab's
    block = CanonicalRow(src_site="x", raw_format="Block", raw_finish="", raw_quality="A")
    normalize.normalize_row(block, resolvers, ref)
    assert block.finish_name == "Raw" and block.finish_method == "block_default_raw"   # block's own rule


def test_synonym_none_finish_gets_last_resort_default(ref):
    # finish 'Other' resolves to "no value" (synonym_none, not an unresolved error). Because finish is
    # required for Medusa, that null then takes the configured last-resort default so the product ships
    # rather than being rejected -- and it is never flagged as an unresolved error.
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="marenostone", raw_format="Slab", raw_finish="Other")
    normalize.normalize_row(row, resolvers, ref)
    assert not any(f.code == FlagCode.attr_unresolved for f in row.review_flags)   # not an error
    assert row.finish_id is not None and row.finish_method == "last_resort_default"   # defaulted, ships


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


# --- compound / multi attribute resolution (resolve_multi) --------------------
def _finish_resolver(extra=()):
    return VocabResolver(vocab="finish",
                         canonical_values=["Polished", "Leathered", "Filled", "Honed",
                                           "Polished and Filled", *extra],
                         synonyms={"leather": "Leathered"}, fuzzy_floor=90.0)


def test_resolve_multi_composes_an_existing_compound_finish():
    r = _finish_resolver()
    assert r.resolve("Polished + Filled").value is None            # whole doesn't resolve
    res = r.resolve_multi("Polished + Filled")                     # ...but the vocab HAS 'Polished and Filled'
    assert res.value == "Polished and Filled" and res.method == "compound"


def test_resolve_multi_suggests_a_new_compound_for_a_finish():
    # 'Polished + Leather' -> no 'Polished and Leathered' in the vocab, but finishes use the 'X and Y'
    # pattern, so SUGGEST the composed compound (not the wrong single fuzzy 'Leathered').
    res = _finish_resolver().resolve_multi("Polished + Leather")
    assert res.value is None and res.method == "compound_suggest"
    assert res.evidence["best"] == "Polished and Leathered"


def test_resolve_multi_strips_descriptive_noise():
    res = _finish_resolver().resolve_multi("Dual Finish - Polished/Leather")
    assert res.method == "compound_suggest" and res.evidence["best"] == "Polished and Leathered"


def test_resolve_multi_never_invents_a_compound_colour():
    # colour has NO 'X and Y' entries -> a list keeps its primary, never composes 'Black and White'
    r = VocabResolver(vocab="color", canonical_values=["Black", "White", "Grey"], synonyms={}, fuzzy_floor=90.0)
    res = r.resolve_multi("Black | White")
    assert res.value == "Black" and res.method == "multi_value"


def test_resolve_multi_single_token_has_nothing_to_split():
    assert _finish_resolver().resolve_multi("Polished").method == "unresolved"


def test_resolve_multi_list_separator_keeps_primary_not_a_false_compound():
    # 'Flamed | Leathered' is ALTERNATIVES (a list), NOT a dual finish -> keep the primary, never compose
    # a false 'Flamed and Leathered' even though finishes DO have the compound pattern. ',' behaves the same.
    r = _finish_resolver(extra=["Flamed"])
    assert r.resolve_multi("Flamed | Leathered").value == "Flamed"
    assert r.resolve_multi("Flamed | Leathered").method == "multi_value"
    assert r.resolve_multi("Flamed, Leathered").method == "multi_value"


def test_resolve_multi_slash_is_a_combination():
    # '/' joins a dual finish ('Honed / Filled' -> 'Honed and Filled'), unlike the list '|'.
    r = _finish_resolver(extra=["Honed and Filled"])
    res = r.resolve_multi("Honed / Filled")
    assert res.value == "Honed and Filled" and res.method == "compound"
