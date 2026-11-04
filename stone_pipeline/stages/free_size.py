"""Free size: a listing whose vendor sells a side 'Free' (cut to order) becomes one listing per shape.

The shapes are the category's long:short ratios in the domain pack (stone tiles: a square, a rectangle and a
long thin one). A side the vendor states is the short side and the free side is ratio x it; with every side
free the long side is the pack's long_side. A shape longer than the pack's max_length is not offered. A
listing with no free side, of a category with no shapes, whose other side is not stated, or with no shape
under the cap, passes through unchanged (its free side stays a defaulted size, as before).

Run right after format resolution (the category decides the shapes) and before matching, so every later stage
sees ordinary listings. Each shape is a copy of the vendor's record with its own key (the record's key plus
its size in centimetres, '-80x40', as a lot's pieces add their position) and the offered size of each free
face; derive reads it (provenance 'free_size_option') and sells the shape made to order. The vendor's record
itself (natural key, raw size text) is untouched.
"""

from __future__ import annotations

from typing import Optional

from stone_pipeline.config.domain import FreeSizeShapes, active_pack
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference.loaders import ReferenceData
from stone_pipeline.stages.derive import dimension_category, is_free_side, parse_face, raw_face_texts
from stone_pipeline.stages.keys_dedupe import KeyCollisionError

# raw face key -> the row field its offered size lands in (raw 'height' is the short face, canonical width)
_OFFER_FIELD = {"length": "free_size_length", "height": "free_size_width"}


def _free_faces(row: CanonicalRow) -> list[str]:
    """The raw face keys the vendor sells 'Free', in _OFFER_FIELD order."""
    texts = raw_face_texts(row)
    return [key for key in _OFFER_FIELD if is_free_side(texts.get(key, ""))]


def _shapes(row: CanonicalRow, ref: ReferenceData) -> list[dict[str, float]]:
    """The listing's shapes, each {raw face key: size in metres} for both faces; [] when it offers none."""
    spec: Optional[FreeSizeShapes] = active_pack().free_size_shapes.get(dimension_category(row))
    free = _free_faces(row)
    if spec is None or not free:
        return []
    if len(free) == len(_OFFER_FIELD):
        sizes = [{"length": spec.long_side, "height": spec.long_side / r} for r in spec.ratios]
    else:
        (stated_key,) = [key for key in _OFFER_FIELD if key not in free]
        stated = parse_face(raw_face_texts(row).get(stated_key, ""), ref)
        if stated is None or stated <= 0:
            return []
        sizes = [{stated_key: stated, free[0]: stated * r} for r in spec.ratios]
    return [{key: round(size, 3) for key, size in shape.items()} for shape in sizes
            if round(max(shape.values()), 3) <= spec.max_length]


def _cm(metres: float) -> str:
    return f"{round(metres * 100):d}"


def run(rows: list[CanonicalRow], ref: ReferenceData) -> tuple[list[CanonicalRow], int]:
    """(the rows with each free-size listing replaced in place by its shapes, how many listings fanned out).
    A shape key that meets another listing's key is a hard error, as in keys_dedupe."""
    out: list[CanonicalRow] = []
    fanned = 0
    for row in rows:
        shapes = _shapes(row, ref)
        if not shapes:
            out.append(row)
            continue
        fanned += 1
        free = _free_faces(row)
        out.extend(row.model_copy(deep=True, update={
            "surrogate_key": f"{row.surrogate_key}-{_cm(shape['length'])}x{_cm(shape['height'])}",
            **{_OFFER_FIELD[key]: shape[key] for key in free},
        }) for shape in shapes)
    seen: set[str] = set()
    for row in out:
        key = (row.surrogate_key or "").upper()
        if key in seen:
            raise KeyCollisionError(f"free-size shape key {key!r} meets another listing's key")
        seen.add(key)
    return out, fanned
