"""Lots: a lot is a set of ordinary products (its pieces) that share one lot_code.

There is no lot record. Each piece is a normal product with its own key, photos, dimensions and stock
(inventory 1), plus two fields Blokport groups on: lot_code (identical on every piece of the lot) and
lot_position (the piece's place in the cut order; gaps are normal, sold pieces are simply absent).

`load` is the loader step, run right after adaptation, and is the ONLY place lot membership is decided.
It serves both source shapes without any source-specific code:
  * PIECE-native (e.g. a cutting-machine API): one record per slab, the adapter maps src_lot_ref (block or
    bundle id) and raw_lot_position (cut sequence). The row gets its lot_code here; nothing is split.
  * BUNDLE-shaped (e.g. a website listing a bundle): the adapter maps raw_pieces, the bundle's per-piece
    photos. The bundle becomes one product per piece ONLY when the evidence is complete: one entry per
    listed piece, each with its own photo, at a unique positive position. Otherwise it stays one product,
    exactly as before (a lot whose pieces share one image set renders and is useless to a buyer).
A source whose `lots` switch is off is left untouched. `summary` counts each lot's pieces for diagnostics.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from typing import Optional

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow

log = logfmt.get_logger("lots")

_NAMESPACE = uuid.UUID(SETTINGS.lots.code_namespace)

# The bundle's quantity figures describe the bundle, not one of its pieces, so a piece does not inherit them.
_BUNDLE_QUANTITY_FIELDS = ("raw_slab_count", "raw_bundle_size", "raw_total_m2", "raw_per_slab_m2",
                           "raw_stock_m2", "raw_slabs_array")


def lot_code(source_code: str, lot_ref: str, label: Optional[str]) -> str:
    """`<label>-<uuid5>`: unique (the uuid covers source + lot ref), byte-identical on every piece, and the
    same on every run. The label is the vendor's printed reference (readable only), else the lot ref."""
    readable = " ".join((label or "").split()) or lot_ref
    return f"{readable}-{uuid.uuid5(_NAMESPACE, f'{source_code}:{lot_ref}')}"


def load(rows: list[CanonicalRow], cfg: SourceConfig) -> list[CanonicalRow]:
    """Establish lot membership for one source's adapted rows (see the module docstring)."""
    if not cfg.lots:
        return rows
    out: list[CanonicalRow] = []
    kept_whole = 0
    for row in rows:
        if row.raw_pieces:                                   # bundle-shaped
            pieces = _split(row, cfg)
            if not pieces:
                kept_whole += 1
            out.extend(pieces or [row])
            continue
        if row.src_lot_ref:                                  # piece-native
            row.lot_code = lot_code(cfg.source_code, row.src_lot_ref, row.src_lot_label)
            row.lot_position = _positive_int(row.raw_lot_position)
        out.append(row)
    lot_count = len({r.lot_code for r in out if r.lot_code})
    log.info("lots loaded", extra={"extra_fields": {
        "source": cfg.source, "lots": lot_count, "pieces": sum(1 for r in out if r.lot_code),
        "bundles_kept_whole": kept_whole}})
    return out


def summary(rows: list[CanonicalRow]) -> list[dict]:
    """Per lot, after validation: how many pieces were loaded, sent, and held (for the diagnostics panel,
    so a lot advertising 75 and showing 73 is visible to the operator before it is to a buyer)."""
    by_code: dict[str, list[CanonicalRow]] = defaultdict(list)
    for row in rows:
        if row.lot_code:
            by_code[row.lot_code].append(row)
    out = []
    for code in sorted(by_code):
        pieces = by_code[code]
        emitted = sum(1 for p in pieces if p.is_emittable)
        out.append({"lot_code": code, "variety": pieces[0].variation_name or None,
                    "pieces": len(pieces), "emitted": emitted, "held": len(pieces) - emitted})
    return out


def replacements(rows: list[CanonicalRow], cfg: SourceConfig) -> dict[str, tuple[str, int]]:
    """Bundle SKU -> (lot_code, pieces) for every lot in the run: the SKU a split bundle carried (its key
    is the lot reference). When that SKU is among the run's discontinued products, the bundle was replaced
    by its lot rather than dropped, and its delist carries the lot (Blokport deactivates it and 301s its
    slug to the lot page). A piece-native lot's reference names no product, so it never matches."""
    out: dict[str, list] = {}
    for row in rows:
        if row.lot_code and row.src_lot_ref:
            entry = out.setdefault(f"{cfg.source_code}-{row.src_lot_ref}".upper(), [row.lot_code, 0])
            entry[1] += 1
    return {sku: (code, pieces) for sku, (code, pieces) in out.items()}


def _split(bundle: CanonicalRow, cfg: SourceConfig) -> list[CanonicalRow]:
    """The bundle's pieces, or [] when the piece evidence is incomplete (the bundle then stays whole)."""
    pieces = _complete_pieces(bundle)
    if not pieces:
        return []
    ref = bundle.src_lot_ref or bundle.src_natural_key
    code = lot_code(cfg.source_code, ref, bundle.src_lot_label)
    own = {url for p in pieces for url in p["image_urls"]}
    shared = [url for url in bundle.raw_image_urls if url not in own]   # never another piece's own photo
    common = {**dict.fromkeys(_BUNDLE_QUANTITY_FIELDS), "src_lot_ref": ref, "raw_pieces": None,
              "raw_inventory_quantity": "1", "lot_code": code}
    return [bundle.model_copy(deep=True, update={
        **common,
        "src_natural_key": f"{bundle.src_natural_key}-{p['position']}",
        "raw_lot_position": str(p["position"]),
        "lot_position": p["position"],
        "raw_image_urls": list(dict.fromkeys([*p["image_urls"], *shared])),   # its own photo first
    }) for p in pieces]


def _complete_pieces(bundle: CanonicalRow) -> list[dict]:
    """The bundle's piece list when it accounts for every listed piece, each with its own photo at a unique
    positive position, and the bundle has a key to derive the pieces' keys from; else []."""
    if not bundle.src_natural_key:
        return []
    try:
        pieces = json.loads(bundle.raw_pieces or "")
    except ValueError:
        log.warning("bundle kept whole: its piece list is not valid JSON",
                    extra={"extra_fields": {"source": bundle.src_site, "key": bundle.src_natural_key}})
        return []
    listed = _positive_int(bundle.raw_slab_count)
    if not isinstance(pieces, list) or listed is None or len(pieces) != listed:
        return []
    positions = [p.get("position") if isinstance(p, dict) else None for p in pieces]
    if not all(type(n) is int and n > 0 for n in positions) or len(set(positions)) != len(positions):
        return []
    if not all(isinstance(p.get("image_urls"), list) and p["image_urls"]
               and all(isinstance(u, str) and u for u in p["image_urls"]) for p in pieces):
        return []
    return sorted(pieces, key=lambda p: p["position"])


def _positive_int(text: Optional[str]) -> Optional[int]:
    """A positive whole number from a raw figure ('5', ' 12 '), else None: a position or a count is never
    guessed from anything less exact."""
    value = (text or "").strip()
    return int(value) if value.isascii() and value.isdigit() and int(value) > 0 else None
