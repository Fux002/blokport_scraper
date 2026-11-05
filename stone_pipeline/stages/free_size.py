"""Free size: a listing whose vendor sells a side 'Free' (cut to order) becomes one listing per shape.

The shapes are the category's long:short ratios in the domain pack (stone tiles: a square, a rectangle and a
long thin one). A side the vendor states is the short side and the long side is ratio x it; with every side
free the long side is the pack's long_side. A shape longer than the pack's max_length is not offered. A
listing with no free side, of a category with no shapes, whose other side is not stated, or with no shape
under the cap, passes through unchanged (its free side stays a defaulted size, as before).

Run right after format resolution (the category decides the shapes) and before matching, so every later stage
sees ordinary listings. Each shape is a copy of the vendor's record with its own key (the record's key plus
its size in centimetres, long side first, '-80x40', as a lot's pieces add their position) and its size, long
side in free_size_length (canonical length is the long face); derive takes both faces from it (provenance
'free_size_shape') and sells the shape made to order. The vendor's record itself (natural key, raw size text)
is untouched.
"""

from __future__ import annotations

from typing import Optional

from stone_pipeline.config.domain import FreeSizeShapes, active_pack, fold_word
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference.loaders import ReferenceData
from stone_pipeline.stages.derive import dimension_category, parse_face, raw_face_texts
from stone_pipeline.stages.keys_dedupe import KeyCollisionError


def _is_free(text: str) -> bool:
    """Whether a raw side value is the pack's word for a side cut to order ('Free')."""
    return fold_word(text) in active_pack().free_size_words


def _shapes(row: CanonicalRow, ref: ReferenceData) -> list[tuple[float, float]]:
    """The listing's shapes, each (long side, short side) in metres; [] when it offers none."""
    spec: Optional[FreeSizeShapes] = active_pack().free_size_shapes.get(dimension_category(row))
    texts = raw_face_texts(row)
    stated = [text for key in ("length", "height") if not _is_free(text := texts.get(key, ""))]
    if spec is None or len(stated) == 2:
        return []
    if not stated:
        sizes = [(spec.long_side, spec.long_side / r) for r in spec.ratios]
    else:
        short = parse_face(stated[0], ref)
        if short is None or short <= 0:
            return []
        sizes = [(short * r, short) for r in spec.ratios]
    return [(round(long, 3), round(short, 3)) for long, short in sizes if round(long, 3) <= spec.max_length]


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
        out.extend(row.model_copy(deep=True, update={
            "surrogate_key": f"{row.surrogate_key}-{_cm(long)}x{_cm(short)}",
            "free_size_length": long,
            "free_size_width": short,
        }) for long, short in shapes)
    seen: set[str] = set()
    for row in out:
        key = (row.surrogate_key or "").upper()
        if key in seen:
            raise KeyCollisionError(f"free-size shape key {key!r} meets another listing's key")
        seen.add(key)
    return out, fanned
