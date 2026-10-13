"""Lots: a lot is a set of ordinary products (its pieces) that share one lot_code.

There is no lot record. Each piece is a normal product with its own key, photos, dimensions and stock, plus
two fields Blokport groups on: lot_code (identical on every piece of the lot) and lot_position (the piece's
place in the cut order; gaps are normal).

`load` is the loader step, run right after adaptation, and is the ONLY place lot membership is decided.
It serves both source shapes without any source-specific code:
  * PIECE-native (e.g. a cutting-machine API): one record per slab, the adapter maps src_lot_ref (block or
    bundle id), raw_lot_position (cut sequence) and the slab's own stock. The row gets its lot_code here.
  * BUNDLE-shaped (e.g. a website listing a bundle): the adapter maps raw_pieces, the bundle's scanned slabs
    (position + own photo), with the bundle's stock (raw_slab_count) and sold record (raw_sold_count). The
    bundle becomes its FULL scanned sequence, a sold slab staying in it at inventory 0, when every slab's
    status is known (`sold_positions`). When it is not, an operator card asks which slabs are sold
    (`cards`); until it is answered a bundle never sent as a lot stays one product, and a lot already in
    Medusa goes OFFLINE, every piece at inventory 0 (it never flips back to a bundle: its old page redirects
    to the lot; and no slab that may be sold stays on offer). The answer brings the right pieces back.
No per-source setting gates any of this: membership follows from the data alone, and Blokport takes a lot
whenever one arrives. `summary` counts each lot's pieces for diagnostics.
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
_BUNDLE_QUANTITY_FIELDS = ("raw_slab_count", "raw_sold_count", "raw_bundle_size", "raw_total_m2",
                           "raw_per_slab_m2", "raw_stock_m2", "raw_slabs_array")

# An operator answer for one lot: the stock figure it was given for, and the slab positions marked sold.
Answer = tuple[int, frozenset[int]]


def lot_code(source_code: str, lot_ref: str, label: Optional[str]) -> str:
    """`<label>-<uuid5>`: unique (the uuid covers source + lot ref), byte-identical on every piece, and the
    same on every run. The label is the vendor's printed reference (readable only), else the lot ref."""
    readable = " ".join((label or "").split()) or lot_ref
    return f"{readable}-{uuid.uuid5(_NAMESPACE, f'{source_code}:{lot_ref}')}"


def sold_positions(positions: list[int], available: Optional[int], sold_known: Optional[int],
                   answer: Optional[Answer]) -> Optional[frozenset[int]]:
    """The scanned slabs that are sold, or None when that is not known. Known when:
      * an operator answered for this stock figure and the answer still leaves no more slabs on offer
        than are in stock; or
      * every scan matches the stock and no sale is on record (all available); or
      * no sale is on record and there are fewer scans than slabs in stock (all scans available; the
        unscanned slabs cannot be offered).
    Anything else (more scans than stock, a sale on record without the slabs it was) is not known."""
    if available is None:
        return None
    if answer is not None and answer[0] == available:
        sold = answer[1] & set(positions)
        return frozenset(sold) if len(positions) - len(sold) <= available else None
    if not sold_known and len(positions) == available:
        return frozenset()
    if sold_known == 0 and len(positions) < available:
        return frozenset()
    return None


def load(rows: list[CanonicalRow], cfg: SourceConfig, answers: Optional[dict[str, Answer]] = None,
         known_skus: frozenset[str] = frozenset()) -> list[CanonicalRow]:
    """Establish lot membership for one source's adapted rows (see the module docstring). `answers` are the
    operator's sold answers by lot reference; `known_skus` the products already in Medusa (upper-case SKUs)."""
    answers = answers or {}
    out: list[CanonicalRow] = []
    kept_whole = offline = 0
    for row in rows:
        if row.raw_pieces:                                   # bundle-shaped
            pieces = _pieces(row)
            sold = sold_positions([p["position"] for p in pieces], _count(row.raw_slab_count),
                                  _count(row.raw_sold_count), answers.get(row.src_natural_key or "")) \
                if pieces else None
            if sold is not None:
                out.extend(_split(row, cfg, pieces, sold))
            elif pieces and _in_medusa(row, cfg, pieces, known_skus):
                offline += 1
                out.extend(_split(row, cfg, pieces, frozenset(p["position"] for p in pieces), unresolved=True))
            else:
                kept_whole += 1
                out.append(row)
            continue
        if row.src_lot_ref:                                  # piece-native
            row.lot_code = lot_code(cfg.source_code, row.src_lot_ref, row.src_lot_label)
            row.lot_position = _positive_int(row.raw_lot_position)
        out.append(row)
    log.info("lots loaded", extra={"extra_fields": {
        "source": cfg.source, "lots": len({r.lot_code for r in out if r.lot_code}),
        "pieces": sum(1 for r in out if r.lot_code), "lots_offline_unresolved": offline, "bundles_kept_whole": kept_whole}})
    return out


def cards(rows: list[CanonicalRow], cfg: SourceConfig, answers: dict[str, Answer]) -> list[dict]:
    """The "which slabs are sold?" cards: one per bundle with a scanned sequence whose sold slabs are not
    known, with every scan to choose from. Shaped for the review queue (ref, payload, sources)."""
    out = []
    for row in rows:
        pieces = _pieces(row) if row.raw_pieces else []
        available, sold_known = _count(row.raw_slab_count), _count(row.raw_sold_count)
        answer = answers.get(row.src_natural_key or "")
        positions = [p["position"] for p in pieces]
        if not pieces or available is None or sold_positions(positions, available, sold_known, answer) is not None:
            continue
        out.append({"ref": f"{cfg.source}|{row.src_natural_key}", "sources": [cfg.source], "payload": {
            "source": cfg.source, "lot_ref": row.src_natural_key, "label": row.src_lot_label,
            "variety": row.raw_name, "src_url": row.src_url, "available": available, "sold_known": sold_known,
            "scanned": len(pieces), "must_mark_sold": max(0, len(pieces) - available),
            "previous_answer": {"available": answer[0], "sold": sorted(answer[1])} if answer else None,
            "pieces": [{"position": p["position"], "image": p["image_urls"][0]} for p in pieces]}})
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


def summary(rows: list[CanonicalRow]) -> list[dict]:
    """Per lot, after validation: its pieces, how many are at inventory 0 (sold, or the whole lot offline
    while its "which slabs are sold?" card is open), sent, and held (for the diagnostics panel, so a lot
    advertising 75 and showing 73 is visible to the operator before a buyer)."""
    by_code: dict[str, list[CanonicalRow]] = defaultdict(list)
    for row in rows:
        if row.lot_code:
            by_code[row.lot_code].append(row)
    out = []
    for code in sorted(by_code):
        pieces = by_code[code]
        emitted = sum(1 for p in pieces if p.is_emittable)
        out.append({"lot_code": code, "variety": pieces[0].variation_name or None, "pieces": len(pieces),
                    "sold": sum(1 for p in pieces if p.inventory_quantity == 0), "emitted": emitted,
                    "held": len(pieces) - emitted, "awaiting_answer": any(p.lot_unresolved for p in pieces)})
    return out


def _split(bundle: CanonicalRow, cfg: SourceConfig, pieces: list[dict], sold: frozenset[int],
           unresolved: bool = False) -> list[CanonicalRow]:
    """The bundle as its full sequence of pieces: a piece in `sold` at inventory 0, every other at 1 (an
    unresolved lot passes every piece as `sold`: offline until answered). Each piece shows its own photo
    first, then the bundle's shared photos (never another piece's own photo)."""
    ref = bundle.src_lot_ref or bundle.src_natural_key
    code = lot_code(cfg.source_code, ref, bundle.src_lot_label)
    own = {url for p in pieces for url in p["image_urls"]}
    shared = [url for url in bundle.raw_image_urls if url not in own]
    common = {**dict.fromkeys(_BUNDLE_QUANTITY_FIELDS), "src_lot_ref": ref, "raw_pieces": None,
              "lot_code": code, "lot_unresolved": unresolved}
    return [bundle.model_copy(deep=True, update={
        **common,
        "src_natural_key": f"{bundle.src_natural_key}-{p['position']}",
        "raw_lot_position": str(p["position"]),
        "lot_position": p["position"],
        "raw_inventory_quantity": "0" if p["position"] in sold else "1",
        "raw_image_urls": list(dict.fromkeys([*p["image_urls"], *shared])),
    }) for p in pieces]


def _pieces(bundle: CanonicalRow) -> list[dict]:
    """The bundle's scanned slabs in cut order when the list is well formed (each slab at a unique positive
    position with its own photo) and the bundle has a key to derive the pieces' keys from; else []."""
    if not bundle.src_natural_key:
        return []
    try:
        pieces = json.loads(bundle.raw_pieces or "")
    except ValueError:
        log.warning("bundle kept whole: its piece list is not valid JSON",
                    extra={"extra_fields": {"source": bundle.src_site, "key": bundle.src_natural_key}})
        return []
    if not isinstance(pieces, list) or not pieces:
        return []
    positions = [p.get("position") if isinstance(p, dict) else None for p in pieces]
    if not all(type(n) is int and n > 0 for n in positions) or len(set(positions)) != len(positions):
        return []
    if not all(isinstance(p.get("image_urls"), list) and p["image_urls"]
               and all(isinstance(u, str) and u for u in p["image_urls"]) for p in pieces):
        return []
    return sorted(pieces, key=lambda p: p["position"])


def _in_medusa(bundle: CanonicalRow, cfg: SourceConfig, pieces: list[dict], known_skus: frozenset[str]) -> bool:
    """Whether this bundle was already sent as a lot: any of its pieces exists in Medusa."""
    return any(f"{cfg.source_code}-{bundle.src_natural_key}-{p['position']}".upper() in known_skus for p in pieces)


def _count(text: Optional[str]) -> Optional[int]:
    """A whole count (0 allowed) from a raw figure, else None: a stock or a sold count is never guessed."""
    value = (text or "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


def _positive_int(text: Optional[str]) -> Optional[int]:
    """A positive whole number from a raw figure ('5', ' 12 '), else None: a position is never guessed."""
    value = _count(text)
    return value if value else None
