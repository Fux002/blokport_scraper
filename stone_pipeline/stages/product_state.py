"""Product existence tracking (item 4): is a scraped product already in Medusa, or
is it new?

Each product has a stable SKU = {source_code}-{surrogate_key}, so identity is the
SKU. The source of truth for "what exists" is a Medusa product export the user
downloads (handle / SKU / inventory). This loads it, tags each emitted row
`new` or `existing` by SKU, and (for existing) flags whether the inventory
changed, which feeds the inventory-only update (item 5).

The loader is tolerant of column naming since the export shape is the user's:
it finds the SKU, handle, and inventory columns case-insensitively.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.numbers import parse_number
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag

log = logfmt.get_logger("product_state")

_SKU_COLS = ("variant sku", "sku")
_INV_COLS = ("inventory quantity", "variant inventory quantity", "inventory_quantity",
             "inventory", "quantity")
_HANDLE_COLS = ("product handle", "handle")


def _pick(row: dict, names: tuple[str, ...]) -> str:
    low = {(k or "").strip().lower(): v for k, v in row.items()}
    for n in names:
        if n in low and (low[n] or "").strip():
            return str(low[n]).strip()
    return ""


@dataclass
class KnownProducts:
    by_sku: dict[str, dict] = field(default_factory=dict)  # SKU(upper) -> {handle, inventory}

    def __len__(self) -> int:
        return len(self.by_sku)


def load_known_products(path: Path | None = None) -> KnownProducts:
    path = Path(path or SETTINGS.paths.products_known_csv)
    known = KnownProducts()
    if not path.exists():
        return known
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for r in csv.DictReader(handle):
            sku = _pick(r, _SKU_COLS).upper()
            if not sku:
                continue
            known.by_sku[sku] = {
                "handle": _pick(r, _HANDLE_COLS),
                "inventory": _pick(r, _INV_COLS),
            }
    return known


def sku_for(row: CanonicalRow, cfg: SourceConfig) -> str:
    return f"{cfg.source_code}-{row.surrogate_key}".upper()


def inventory_for(row: CanonicalRow) -> str:
    """The product's stock quantity. A positive parseable count wins ('1,000'->1000, '1.0'->1,
    '12 pcs'->12). With NO positive signal the product is OUT OF STOCK -> '0', never a guessed '1':
    stock NEVER gates whether a product is prepared and published (a 0-stock item still ships and
    becomes sellable when a later scrape restocks it -- availability is the inventory step's job).
    A literal 0 is trusted; an UNPARSEABLE stock is surfaced via a review flag in classify() rather
    than silently defaulted, so a supplier format change can never masquerade as 'available'."""
    for candidate in (row.raw_slab_count, row.bundle_size, row.raw_inventory_quantity):
        text = str(candidate).strip() if candidate is not None else ""
        n = parse_number(text)
        if n is not None and int(n) > 0:
            return str(int(n))
    return "0"


def _stock_is_unparseable(row: CanonicalRow) -> bool:
    """True when a stock field was present but no candidate parsed to a number -- a supplier format
    change we should surface (flagged in classify), distinct from a genuine 0 / absent field."""
    present = [str(c).strip() for c in (row.raw_slab_count, row.bundle_size, row.raw_inventory_quantity)
               if c is not None and str(c).strip()]
    return bool(present) and all(parse_number(t) is None for t in present)


@dataclass
class ClassifyStats:
    new: int = 0
    existing: int = 0
    inventory_changed: int = 0


def classify(rows: list[CanonicalRow], cfg: SourceConfig, known: KnownProducts) -> ClassifyStats:
    """Tag each row new/existing by SKU; for existing, flag inventory change."""
    stats = ClassifyStats()
    for row in rows:
        if _stock_is_unparseable(row):
            row.add_flag(ReviewFlag(field="inventory", code=FlagCode.stock_unparseable,
                                    detail="stock field present but unparseable; shipped out-of-stock (0)"))
        sku = sku_for(row, cfg)
        entry = known.by_sku.get(sku)
        if entry is None:
            row.product_status = "new"
            stats.new += 1
        else:
            row.product_status = "existing"
            stats.existing += 1
            # Compare like-for-like: new_inv is normalized (inventory_for -> str(int(...))), so the export's
            # raw value must be normalized the SAME way or messy formatting ('10.0', '1,000', ' 10 ') reads
            # as a phantom change every run -- emitting spurious inventory deltas and breaking byte-identical
            # re-runs. A real 0 is preserved (inventory_for floors to 0, not 1: no positive stock = out of stock).
            old_raw = (entry.get("inventory") or "").strip()
            _old_n = parse_number(old_raw) if old_raw else None
            old_inv = str(int(_old_n)) if _old_n is not None else old_raw
            new_inv = inventory_for(row)
            row.product_changed = bool(old_inv) and old_inv != new_inv
            if row.product_changed:
                stats.inventory_changed += 1
    log.info("product status", extra={"extra_fields": {
        "new": stats.new, "existing": stats.existing, "inventory_changed": stats.inventory_changed,
        "known": len(known)}})
    return stats


def discontinued(rows: list[CanonicalRow], cfg: SourceConfig, known: KnownProducts) -> list[tuple[str, str]]:
    """Close the delete loop: Medusa SKUs owned by THIS source (sku prefix `{source_code}-`) that
    the latest scrape did NOT produce -- the supplier dropped them. Returned as (sku, handle) for a
    reversible stock-0 delist + a review report. Empty without a baseline (`known`), so it never
    fires until the Medusa product export is present. Source-scoped by SKU prefix, so one supplier's
    run can never delist another's products."""
    if not known.by_sku:
        return []
    scraped = {sku_for(r, cfg) for r in rows}
    prefix = f"{cfg.source_code}-".upper()
    return [(sku, (entry.get("handle") or "")) for sku, entry in known.by_sku.items()
            if sku.startswith(prefix) and sku not in scraped]
