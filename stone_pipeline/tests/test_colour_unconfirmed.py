"""No guessed colour (Mas's word, 10 Nov; C5's "black marble" finding). A product of a variety sold in several colours
used to get ONE of them by "first wins" in two places: reconcile filled the variety's first allowed colour when the
vendor gave none (Patagonia Quartzite -> Black), and normalize kept the first of a vendor's list ('Black and White' ->
Black). Now:

1. a vendor colour text naming two or more distinct colours is the pack's multicolour value ('Multicolor');
2. no vendor colour and a variety that allows several: the operator's colour statement for (source, variety) decides;
   without one the colour is UNCONFIRMED: a NEW listing is held, an already-published one stays listed with the colour
   it has (loads never remove) and is queued; curate raises ONE pending_confirm card per (source, variety, type);
3. a variety with ONE allowed colour is unchanged. The backbone's colour LIST is never multicolour evidence.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference import loaders
from stone_pipeline.stages import normalize, reconcile_tree, validate


@pytest.fixture
def ref():
    return loaders.load_all()


@pytest.fixture
def patagonia(ref):
    variety = ref.backbone.lookup("Patagonia", stone_type="Quartzite")
    assert variety is not None and len(variety.colors) > 1          # sold in several colours
    return variety


def _row(variety, **kw):
    base = dict(src_site="varsha", surrogate_key="1056", variation_id="vid", variation_name=variety.variant,
                variety_match_key=variety.variant, type_name=variety.stone_type, color_name=None,
                finish_name=variety.finishes[0], quality_name=variety.qualities[0])
    base.update(kw)
    return CanonicalRow(**base)


def _reconcile(ref, row):
    reconcile_tree.reconcile_row(row, ref, reconcile_tree.ReconcileStats())
    return row


def _unconfirmed(row) -> bool:
    return any(f.code == FlagCode.colour_unconfirmed for f in row.review_flags)


# --- rule 1: the vendor's own text names several colours ------------------------------------------------------------

def test_a_vendor_text_naming_two_colours_is_multicolor(ref):
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", surrogate_key="742", raw_color="Black and White")
    normalize.normalize_row(row, resolvers, ref)
    assert row.color_name == "Multicolor" and row.color_method == "multicolor_from_vendor"
    assert row.color_id is not None
    assert not any(f.code == FlagCode.multi_value and f.field == "color" for f in row.review_flags)


def test_one_colour_named_twice_is_not_multicolor(ref):
    resolvers = normalize.AttributeResolvers.build(ref)
    row = CanonicalRow(src_site="polonine", surrogate_key="743", raw_color="White, white")
    normalize.normalize_row(row, resolvers, ref)
    assert row.color_name == "White"


# --- rule 2: no vendor colour, a variety sold in several -------------------------------------------------------------

def test_no_colour_and_a_multi_colour_variety_is_unconfirmed(ref, patagonia):
    row = _reconcile(ref, _row(patagonia))
    assert row.color_method == "colour_unconfirmed" and _unconfirmed(row)
    assert row.color_name == patagonia.colors[0]                    # the colour a published listing already carries


def test_a_new_unconfirmed_row_is_held(ref, patagonia):
    row = _reconcile(ref, _row(patagonia))
    validate.validate_row(row, source_code="var", published=frozenset())
    assert any(r.rule == "colour_unconfirmed" for r in row.reject_reasons)


def test_a_published_unconfirmed_row_stays_listed_and_is_queued(ref, patagonia):
    row = _reconcile(ref, _row(patagonia))
    validate.validate_row(row, source_code="var", published=frozenset({"VAR-1056"}))
    assert not any(r.rule == "colour_unconfirmed" for r in row.reject_reasons)
    assert row.color_name == patagonia.colors[0] and _unconfirmed(row)


def test_a_decision_updates_every_row_of_its_source_and_variety(ref, patagonia):
    ref.decisions = Decisions.from_statements(
        [{"source": "varsha", "spelling_norm": "patagonia", "spelling": "Patagonia", "verdict": "is",
          "name": "Patagonia", "stone_type": "Quartzite", "color": "White", "origin_iso": None, "widen": 0,
          "asked_by": "varsha"}],
        exists_as=lambda name, stone_type: True, alias_target=lambda name, stone_type: None)
    one = _reconcile(ref, _row(patagonia, surrogate_key="1056", variety_match_key="Patagonia"))
    two = _reconcile(ref, _row(patagonia, surrogate_key="2230", variety_match_key="Patagonia Quartzite Polished"))
    other = _reconcile(ref, _row(patagonia, src_site="sentas", surrogate_key="9"))
    assert [(r.color_name, r.color_method, _unconfirmed(r)) for r in (one, two)] == [("White", "operator", False)] * 2
    assert other.color_method == "colour_unconfirmed"                # another vendor's listings are its own decision


def test_curate_raises_one_card_per_source_variety_and_type(ref, patagonia):
    from stone_pipeline.stages.curate import review
    rows = [_reconcile(ref, _row(patagonia, surrogate_key=k)) for k in ("1056", "1217")]
    cards = review.colour_cards(rows)
    assert [(c["kind"], c["variant"], c["stone_type"], c["src"], c["scraped"]) for c in cards] == [
        ("colour_unconfirmed", "Patagonia", "Quartzite", "varsha", "Patagonia")]
    assert "colour" in cards[0]["reason"]


# --- rule 3: one allowed colour is unchanged -----------------------------------------------------------------------

def test_a_single_colour_variety_is_unchanged(ref):
    single = next(v for vs in ref.backbone.by_norm_name.values() for v in vs if len(v.colors) == 1)
    row = _reconcile(ref, _row(single))
    assert (row.color_name, row.color_method, row.color_confidence) == (single.colors[0], "from_variety", "high")
    assert not _unconfirmed(row)
