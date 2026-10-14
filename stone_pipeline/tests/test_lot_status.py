"""Which slabs of a lot are sold: one rule, in the loader.

The measured slabs are listed; a sold slab stays in the lot at inventory 0, and a scan the source does not
measure is never offered. Which slabs are sold is KNOWN when:
  * nothing is sold (the packing list records 0 sold, or there is none and every scan matches the
    stock) and no more slabs are offered than are in stock (all offered slabs available), or
  * an operator answered which slabs are sold, for the CURRENT stock figure.
Otherwise it is unknown and an operator card asks "which slabs are sold?". Until it is answered, a bundle
that was never sent as a lot stays one bundle product; a lot whose pieces already exist in Medusa is held
whole (it never flips back to a bundle: its old page already redirects to the lot).
"""

from __future__ import annotations

import json

import pytest

from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.stages import derive, lots, validate

CFG = SourceConfig(source="sentas", source_code="sen")


def _bundle(positions, available, sold_known=None, key="2351", unmeasured=()):
    pieces = [{"position": n, "image_urls": [f"scan-{n}"],
               **({} if n in unmeasured else {"raw_dimensions": f"length=30{n} cm;height=180 cm", "raw_weight": "300.0"})}
              for n in positions]
    return CanonicalRow(src_site="sentas", src_natural_key=key, raw_name="Golden Emparador", src_lot_label="B4804",
                        raw_slab_count=None if available is None else str(available),
                        raw_sold_count=None if sold_known is None else str(sold_known),
                        raw_image_urls=["g1", *(f"scan-{n}" for n in positions)], raw_pieces=json.dumps(pieces))


def _stock(rows):
    return {r.lot_position: r.raw_inventory_quantity for r in rows}


# -- the status rule ------------------------------------------------------------------------------------

def test_every_scan_matching_the_stock_makes_every_slab_available():
    out = lots.load([_bundle([1, 2, 3], available=3)], CFG)
    assert _stock(out) == {1: "1", 2: "1", 3: "1"} and not any(r.lot_unresolved for r in out)


def test_nothing_sold_on_record_with_fewer_scans_lists_every_scan_as_available():
    out = lots.load([_bundle([1, 2, 3], available=5, sold_known=0)], CFG)
    assert _stock(out) == {1: "1", 2: "1", 3: "1"}                       # 2 unscanned slabs not offered


@pytest.mark.parametrize("positions, available, sold_known", [
    ([1, 2, 3, 4], 2, 2),        # 2 slabs sold, but not which
    ([1, 2, 3, 4], 2, None),     # more scans than stock, nothing on record
    ([1, 2, 3], 5, None),        # fewer scans than stock, sold status unknown
    ([1, 2, 3], 3, 1),           # scans match the stock but a sale is on record: ambiguous
])
def test_unknown_status_keeps_a_never_sent_bundle_whole(positions, available, sold_known):
    row = _bundle(positions, available, sold_known)
    out = lots.load([row], CFG)
    assert out == [row] and row.lot_code is None


def test_a_record_of_nothing_sold_lists_every_measured_slab_up_to_the_stock():
    # B4306: the packing list says 26 in stock, none sold; 27 scans, one of them not a measured slab
    out = lots.load([_bundle([1, 2, 3, 4], available=3, sold_known=0, unmeasured=(4,))], CFG)
    assert _stock(out) == {1: "1", 2: "1", 3: "1"}


def test_more_measured_slabs_than_the_stock_is_never_offered_on_a_record_of_nothing_sold():
    row = _bundle([1, 2, 3, 4], available=3, sold_known=0)
    assert lots.load([row], CFG) == [row] and row.lot_code is None        # contradictory: asked, not guessed


def test_an_answer_for_the_current_stock_lists_the_full_sequence_with_the_sold_slabs_at_zero():
    answers = {"2351": (2, frozenset({1, 3}))}
    out = lots.load([_bundle([1, 2, 3, 4], available=2, sold_known=2)], CFG, answers=answers)
    assert _stock(out) == {1: "0", 2: "1", 3: "0", 4: "1"}
    piece = next(r for r in out if r.lot_position == 1)
    derive.derive_inventory(piece)
    assert piece.inventory_quantity == 0                                  # a sold piece ships at inventory 0


def test_an_answer_given_for_another_stock_figure_is_stale_and_asked_again():
    answers = {"2351": (3, frozenset({1}))}                              # answered when 3 were in stock
    row = _bundle([1, 2, 3, 4], available=2, sold_known=2)
    assert lots.load([row], CFG, answers=answers) == [row]


def test_an_answer_that_would_offer_more_slabs_than_are_in_stock_is_not_used():
    answers = {"2351": (2, frozenset({1}))}                              # 3 left unmarked, only 2 in stock
    row = _bundle([1, 2, 3, 4], available=2, sold_known=2)
    assert lots.load([row], CFG, answers=answers) == [row]


# -- a lot already in Medusa goes offline, never turned back into a bundle -----------------------------

def test_an_established_lot_with_unknown_status_goes_offline_until_answered():
    known = frozenset({"SEN-2351-1"})                                     # a piece already exists in Medusa
    out = lots.load([_bundle([1, 2, 3, 4], available=2, sold_known=2)], CFG, known_skus=known)
    assert len(out) == 4 and all(r.lot_unresolved and r.lot_code for r in out)
    assert _stock(out) == {1: "0", 2: "0", 3: "0", 4: "0"}               # no slab that may be gone on offer
    answered = lots.load([_bundle([1, 2, 3, 4], available=2, sold_known=2)], CFG, known_skus=known,
                         answers={"2351": (2, frozenset({2, 4}))})
    assert _stock(answered) == {1: "1", 2: "0", 3: "1", 4: "0"} and not any(r.lot_unresolved for r in answered)


def test_an_established_lot_goes_offline_even_when_no_scan_is_measured_this_run():
    # its cutting list missing this run: the pieces already in Medusa still go to zero, never back to a bundle
    known = frozenset({"SEN-2351-1"})
    out = lots.load([_bundle([1, 2], available=2, unmeasured=(1, 2))], CFG, known_skus=known)
    assert _stock(out) == {1: "0", 2: "0"} and all(r.lot_unresolved for r in out)


def _emittable(piece):
    piece.surrogate_key = piece.src_natural_key
    for name, value in dict(variation_key="k", variation_id="V", type_name="Marble", type_id="T", color_id="C",
                            finish_id="F", quality_id="Q", category_pcat_id="PC", handle=piece.surrogate_key,
                            slug=piece.surrogate_key, company_id="CO", sales_channel_id="SC",
                            origin_country_code="TR", length=3.1, width=1.9, height=0.02, weight=0.3,
                            inventory_quantity=1, product_image_keys=["i"], thumbnail_key="i").items():
        setattr(piece, name, value)
    return piece


def test_an_offline_lot_is_still_sent_so_its_pieces_really_go_offline():
    # held back from the sync, the pieces would stay live in Medusa at their last stock
    known = frozenset({"SEN-2351-1"})
    pieces = [_emittable(p) for p in lots.load([_bundle([1, 2], available=1, sold_known=1)], CFG, known_skus=known)]
    for p in pieces:
        derive.derive_inventory(p)
    validate.run(pieces, emit_on_review=True)
    assert all(p.inventory_quantity == 0 and not any(r.rule == "lot_inconsistent" for r in p.reject_reasons)
               for p in pieces)
    assert lots.summary(pieces)[0]["awaiting_answer"] is True


# -- the card that asks which slabs are sold ----------------------------------------------------------

def test_a_card_asks_which_slabs_are_sold_with_every_scan_to_choose_from():
    cards = lots.cards([_bundle([1, 2, 3, 4], available=2, sold_known=2)], CFG, answers={})
    assert cards == [{"ref": "sentas|2351", "sources": ["sentas"], "payload": {
        "source": "sentas", "lot_ref": "2351", "label": "B4804", "variety": "Golden Emparador", "src_url": None,
        "available": 2, "sold_known": 2, "scanned": 4, "must_mark_sold": 2, "previous_answer": None,
        "pieces": [{"position": n, "image": f"scan-{n}"} for n in (1, 2, 3, 4)]}}]


def test_a_card_asks_about_the_vendor_s_full_scanned_sequence():
    # the sold slabs are counted against the vendor's stock, which counts every scanned slab
    cards = lots.cards([_bundle([1, 2, 3, 4, 5], available=2, sold_known=3, unmeasured=(5,))], CFG, answers={})
    payload = cards[0]["payload"]
    assert [p["position"] for p in payload["pieces"]] == [1, 2, 3, 4, 5]
    assert (payload["scanned"], payload["must_mark_sold"]) == (5, 3)


def test_an_answer_lists_only_the_measured_slabs_with_the_sold_ones_at_zero():
    row = _bundle([1, 2, 3, 4, 5], available=2, sold_known=3, unmeasured=(5,))
    out = lots.load([row], CFG, answers={"2351": (2, frozenset({1, 3, 5}))})
    assert _stock(out) == {1: "0", 2: "1", 3: "0", 4: "1"}                # 5: sold and unmeasured, not listed


def test_a_bundle_with_no_measured_slab_raises_no_card():
    # it can never become a lot (no slab could be offered), so there is nothing to answer
    row = _bundle([1, 2, 3, 4], available=2, sold_known=2, unmeasured=(1, 2, 3, 4))
    assert lots.cards([row], CFG, answers={}) == []


def test_a_stale_answer_is_shown_on_the_card_and_a_known_status_raises_no_card():
    stale = lots.cards([_bundle([1, 2, 3, 4], available=2, sold_known=2)], CFG, answers={"2351": (3, frozenset({1}))})
    assert stale[0]["payload"]["previous_answer"] == {"available": 3, "sold": [1]}
    assert lots.cards([_bundle([1, 2, 3], available=3)], CFG, answers={}) == []


# -- diagnostics ----------------------------------------------------------------------------------------

def test_the_summary_counts_sold_pieces_apart():
    pieces = [_emittable(p) for p in
              lots.load([_bundle([1, 2, 3], available=2, sold_known=1)], CFG, answers={"2351": (2, frozenset({2}))})]
    pieces[1].inventory_quantity = 0
    summary = lots.summary(pieces)[0]
    assert (summary["pieces"], summary["sold"], summary["emitted"], summary["held"]) == (3, 1, 3, 0)
