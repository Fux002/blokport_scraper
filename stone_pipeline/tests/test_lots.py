"""Lots: a lot is a set of ordinary products (pieces) that share a lot_code, each sold on its own.

The loader step (stages/lots.load) establishes lot membership for both source shapes:
  * a BUNDLE-shaped source (a website listing a bundle with a per-piece photo list) is expanded into one
    product per piece: a slab with its own photo and its own measured size; a bundle with no such slab
    stays one product;
  * a PIECE-native source (a cutting-machine API: one record per slab with its block id and cut sequence)
    is mapped 1:1 by its adapter and only gets its lot_code here, with no source-specific code.
Downstream: a piece is a single item (derive), a lot's pieces must agree with each other (validate), a
lot is one listing for near-duplicate detection (keys), and diagnostics count each lot's pieces.
"""

from __future__ import annotations

import dataclasses
import json
import uuid

import polars as pl
import pytest

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core.schema import CanonicalRow, FlagCode, RejectReason
from stone_pipeline.stages import derive, keys_dedupe, lots, validate

CFG = SourceConfig(source="sentas", source_code="sen")


def _bundle(pieces, *, slab_count=None, key="7149", images=("g1", "g2", "s1", "s2"), **kw):
    base = dict(src_site="sentas", src_natural_key=key, src_url="https://x/p/7149", raw_name="Calacatta Viola",
                variety_match_key="Calacatta Viola", src_lot_label="B6392", raw_format="slab",
                raw_dimensions="length=317 cm;height=198 cm", raw_thickness="2 cm", raw_weight="345.2",
                raw_total_m2="489.57", raw_slab_count=str(len(pieces) if slab_count is None else slab_count),
                raw_image_urls=list(images), raw_pieces=json.dumps(pieces) if pieces is not None else "")
    base.update(kw)
    return CanonicalRow(**base)


def _slab(position, *urls, size="length=306 cm;height=153 cm", kg="291.0"):
    """A scanned slab as the adapter declares it: its photo(s), and its own measured size and weight
    (size=None: a scan the source does not measure)."""
    measured = {"raw_dimensions": size, "raw_weight": kg} if size is not None else {}
    return {"position": position, "image_urls": list(urls), **measured}


TWO = [_slab(1, "s1"), _slab(4, "s2", size="length=307 cm;height=160 cm", kg="309.0")]


def _code(source_code, ref, label):
    return f"{label}-{uuid.uuid5(uuid.UUID(SETTINGS.lots.code_namespace), f'{source_code}:{ref}')}"


# -- the namespace is fixed once ----------------------------------------------------------------------------

def test_the_lot_code_namespace_never_changes():
    # changing it re-keys every lot on the next produce (each would appear as a new lot to Blokport)
    assert SETTINGS.lots.code_namespace == "be1ef5d0-ee2e-4157-b6ec-8870c49e7deb"


# -- loader: bundle-shaped sources --------------------------------------------------------------------------

def test_a_bundle_with_one_photo_per_listed_piece_becomes_one_product_per_piece():
    pieces = lots.load([_bundle(TWO)], CFG)
    assert [p.src_natural_key for p in pieces] == ["7149-1", "7149-4"]
    assert [p.lot_position for p in pieces] == [1, 4]                       # the vendor's slab numbers, gaps kept
    assert {p.lot_code for p in pieces} == {_code("sen", "7149", "B6392")}  # byte-identical on every piece
    assert all(p.src_lot_ref == "7149" for p in pieces)
    # each piece: its own photo first, then the bundle's shared photos (never another piece's own photo)
    assert pieces[0].raw_image_urls == ["s1", "g1", "g2"] and pieces[1].raw_image_urls == ["s2", "g1", "g2"]
    # a piece is one item: inventory 1, the bundle's quantity figures are not facts about the piece
    assert all(p.raw_inventory_quantity == "1" and p.raw_slab_count is None and p.raw_total_m2 is None
               and p.raw_pieces is None for p in pieces)
    # each piece carries its OWN measured size and weight; the thickness the bundle states carries over
    assert [(p.raw_dimensions, p.raw_weight) for p in pieces] == [("length=306 cm;height=153 cm", "291.0"),
                                                                  ("length=307 cm;height=160 cm", "309.0")]
    assert all(p.raw_thickness == "2 cm" for p in pieces)


def test_the_lot_code_is_deterministic_across_runs():
    first = lots.load([_bundle(TWO)], CFG)[0].lot_code
    again = lots.load([_bundle(TWO)], CFG)[0].lot_code
    other_source = lots.load([_bundle(TWO)], SourceConfig(source="other", source_code="oth"))[0].lot_code
    assert first == again and first != other_source


def test_the_label_defaults_to_the_lot_ref_and_is_whitespace_normalised():
    assert lots.load([_bundle(TWO, src_lot_label=None)], CFG)[0].lot_code == _code("sen", "7149", "7149")
    assert lots.load([_bundle(TWO, src_lot_label="  B 63\t92 ")], CFG)[0].lot_code == _code("sen", "7149", "B 63 92")


@pytest.mark.parametrize("pieces, slab_count", [
    (TWO, 3),                                                              # a listed piece has no photo
    ([_slab(1, "s1"), _slab(1, "s2")], None),                               # duplicate position
    ([_slab(1), _slab(2, "s2")], None),                                     # a piece without a photo
    ([_slab(0, "s1")], None),                                               # positions start at 1
    ([{**_slab(1, "s1"), "position": "1"}], None),                          # not an integer
    ([{**_slab(1, "s1"), "raw_dimensions": 306}], None),                    # a size that is not raw text
    (TWO, ""),                                                              # the listed count is unknown
    ([_slab(1, "s1", size=None), _slab(4, "s2", size=None)], None),         # no scan is measured
])
def test_a_bundle_without_complete_piece_evidence_stays_one_product(pieces, slab_count):
    row = _bundle(pieces, slab_count=slab_count)
    out = lots.load([row], CFG)
    assert len(out) == 1 and out[0].src_natural_key == "7149" and out[0].lot_code is None


def test_a_scan_the_source_does_not_measure_is_never_offered():
    # e.g. a void entry on the vendor's cutting list (no size, a stand-in photo): not a slab that can be sold
    pieces = [*TWO, _slab(7, "s7", size=None)]
    out = lots.load([_bundle(pieces, slab_count=3)], CFG)
    assert [p.lot_position for p in out] == [1, 4]                          # the measured slabs; 7 is a gap


def test_which_slabs_are_sold_is_read_on_the_vendor_s_full_scanned_sequence():
    # The vendor counts a void slab in its stock (B6244: 65 scans, 65 in stock, one void, no packing list):
    # every scan matches the stock and nothing is sold, so the 64 measured slabs are all available.
    pieces = [*TWO, _slab(7, "s7", size=None)]
    out = lots.load([_bundle(pieces, slab_count=3)], CFG, known_skus=frozenset({"SEN-7149-1"}))
    assert [(p.lot_position, p.raw_inventory_quantity) for p in out] == [(1, "1"), (4, "1")]
    assert not any(p.lot_unresolved for p in out)                           # never taken offline for a void


def test_malformed_piece_data_keeps_the_bundle_whole():
    row = _bundle(TWO)
    row.raw_pieces = "[{not json"
    out = lots.load([row], CFG)
    assert len(out) == 1 and out[0].lot_code is None


def test_a_bundle_without_a_natural_key_is_never_split():
    out = lots.load([_bundle(TWO, key=None)], CFG)
    assert len(out) == 1 and out[0].lot_code is None


def test_lot_membership_is_decided_by_the_data_alone_with_no_per_source_switch():
    # Blokport takes a lot whenever one arrives, so no source setting gates the loader
    assert "lots" not in {f.name for f in dataclasses.fields(SourceConfig)}
    out = lots.load([_bundle(TWO)], SourceConfig(source="sentas", source_code="sen"))
    assert [p.lot_position for p in out] == [1, 4] and len({p.lot_code for p in out}) == 1


# -- loader: piece-native sources (e.g. a cutting-machine API) ---------------------------------------------

class _CuttingMachineAdapter(AdapterBase):
    """A synthetic piece-native source: one record per slab, as a cutting-machine API would deliver it."""
    source = "machine"
    adapter_version = "1.0.0"
    variety_match_key = "stone"
    required_columns = ["slab_id", "block_id", "sequence"]
    required_canonical = ("src_natural_key",)
    field_map = {
        "src_natural_key": "slab_id",
        "src_lot_ref": "block_id",
        "raw_lot_position": "sequence",
        "raw_name": "stone",
        "raw_dimensions": lambda r: AdapterBase.build_dims(r.get("length_m"), r.get("width_m"), unit="m"),
        "raw_thickness": "thickness",
        "raw_inventory_quantity": lambda r: "1",
        "raw_image_urls": lambda r: [r["scan_url"]],
    }


def _machine_frame():
    return pl.DataFrame([
        {"slab_id": f"S{n}", "block_id": "BLK-88", "sequence": str(n), "stone": "Calacatta Viola",
         "length_m": l, "width_m": w, "thickness": "2cm", "scan_url": f"https://m/{n}.jpg"}
        for n, l, w in [(1, "3.10", "1.95"), (2, "3.08", "1.94"), (3, "3.12", "1.96")]
    ])


def test_a_piece_native_source_forms_a_lot_with_no_source_specific_code():
    rows = _CuttingMachineAdapter().adapt(_machine_frame())
    out = lots.load(rows, SourceConfig(source="machine", source_code="mac"))
    assert len(out) == 3                                                    # mapped 1:1, nothing expanded
    assert {r.lot_code for r in out} == {_code("mac", "BLK-88", "BLK-88")}
    assert [r.lot_position for r in out] == [1, 2, 3]
    # each piece keeps its own measured size and its own scan
    assert [r.raw_dimensions for r in out] == ["length=3.10m;height=1.95m", "length=3.08m;height=1.94m",
                                               "length=3.12m;height=1.96m"]
    assert [r.raw_image_urls for r in out] == [["https://m/1.jpg"], ["https://m/2.jpg"], ["https://m/3.jpg"]]


def test_a_piece_native_position_that_is_not_a_positive_integer_is_left_unset():
    row = CanonicalRow(src_site="machine", src_natural_key="S1", src_lot_ref="BLK-88", raw_lot_position="x")
    out = lots.load([row], SourceConfig(source="machine", source_code="mac"))
    assert out[0].lot_code and out[0].lot_position is None                 # validate holds the lot (below)


# -- downstream: a piece is a single item ----------------------------------------------------------------

def test_a_lot_piece_is_sold_singly_never_as_a_bundle():
    piece = lots.load([_bundle(TWO)], CFG)[0]
    piece.surrogate_key, piece.type_name = "7149-1", "Marble"
    derive.derive_bundle_size(piece, None, CFG)
    derive.derive_inventory(piece)
    assert piece.sold_in_bundle is False and piece.bundle_size is None and piece.bundle_size_method == "lot_piece"
    assert piece.inventory_quantity == 1
    assert not any(f.field == "bundle_size" for f in piece.review_flags)


# -- downstream: a lot is one listing for near-duplicate detection ----------------------------------------

def _near_dup(row):
    return any(f.code == FlagCode.near_duplicate for f in row.review_flags)


def test_pieces_of_one_lot_are_not_near_duplicates_of_each_other():
    pieces = lots.load([_bundle(TWO)], CFG)
    keys_dedupe.run(pieces)
    assert not any(_near_dup(p) for p in pieces)


def test_a_second_lot_of_the_same_stone_is_flagged_once_like_a_second_bundle():
    first = lots.load([_bundle(TWO)], CFG)
    second = lots.load([_bundle(TWO, key="9000", src_lot_label="B9000")], CFG)
    keys_dedupe.run(first + second)
    assert [_near_dup(p) for p in first + second] == [False, False, True, False]


# -- downstream: the lot gate in validate -------------------------------------------------------------------

def _emittable_piece(code="L-1", position=1, variation_key="slab_marble_cv_1", qty=1, **kw):
    base = dict(src_site="sentas", surrogate_key=f"7149-{position}", lot_code=code, lot_position=position,
                variation_key=variation_key, variation_id="V1", type_name="Marble", type_id="T1",
                color_id="C1", finish_id="F1", quality_id="Q1", category_pcat_id="PC1",
                handle=f"h-{position}", slug=f"h-{position}", company_id="CO", sales_channel_id="SC",
                origin_country_code="TR", length=3.1, width=1.9, height=0.02, weight=0.3,
                inventory_quantity=qty, product_image_keys=["i"], thumbnail_key="i",
                variation_name="Calacatta Viola")
    base.update(kw)
    return CanonicalRow(**base)


def _lot_rejects(rows):
    validate.run(rows, emit_on_review=True)
    return [[r.detail for r in row.reject_reasons if r.rule == "lot_inconsistent"] for row in rows]


def test_a_consistent_lot_passes_the_lot_gate():
    assert _lot_rejects([_emittable_piece(position=1), _emittable_piece(position=2)]) == [[], []]


@pytest.mark.parametrize("second, detail", [
    (dict(position=1), "position_duplicate"),
    (dict(position=None), "position_missing"),
    (dict(variation_key="slab_marble_other_2"), "identity_differs"),
    (dict(type_name="Onyx"), "identity_differs"),
    (dict(category_pcat_id="PC2"), "identity_differs"),
    (dict(qty=2), "quantity_above_one"),
])
def test_a_lot_whose_pieces_disagree_is_held_whole(second, detail):
    first = _emittable_piece(position=1)
    other = _emittable_piece(**{"position": 2, **second})
    assert _lot_rejects([first, other]) == [[detail], [detail]]            # never half a lot published


def test_a_lot_code_longer_than_blokport_accepts_is_held():
    code = "x" * (SETTINGS.lots.max_code_length + 1)
    assert _lot_rejects([_emittable_piece(code=code)]) == [["code_too_long"]]


def test_rows_outside_any_lot_are_untouched_by_the_lot_gate():
    plain = _emittable_piece(code=None, position=None)
    assert _lot_rejects([plain]) == [[]]


# -- diagnostics: pieces sent against pieces held, per lot ------------------------------------------------

def test_the_lot_summary_counts_each_lot_s_pieces():
    held = _emittable_piece(position=2)
    held.add_reject(RejectReason(rule="no_image", detail=""))                # a piece held for its own reason
    rows = [_emittable_piece(position=1), held, _emittable_piece(code=None, position=None)]
    assert lots.summary(rows) == [{"lot_code": "L-1", "variety": "Calacatta Viola",
                                   "pieces": 2, "sold": 0, "emitted": 1, "held": 1,
                                   "awaiting_answer": False}]
