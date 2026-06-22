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
from stone_pipeline.core.schema import CanonicalRow

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
    for candidate in (row.raw_slab_count, row.bundle_size, row.raw_inventory_quantity):
        if candidate and str(candidate).strip().isdigit():
            return str(int(str(candidate)))
    return "1"


@dataclass
class ClassifyStats:
    new: int = 0
    existing: int = 0
    inventory_changed: int = 0


def classify(rows: list[CanonicalRow], cfg: SourceConfig, known: KnownProducts) -> ClassifyStats:
    """Tag each row new/existing by SKU; for existing, flag inventory change."""
    stats = ClassifyStats()
    for row in rows:
        sku = sku_for(row, cfg)
        entry = known.by_sku.get(sku)
        if entry is None:
            row.product_status = "new"
            stats.new += 1
        else:
            row.product_status = "existing"
            stats.existing += 1
            old_inv = (entry.get("inventory") or "").strip()
            new_inv = inventory_for(row)
            row.product_changed = bool(old_inv) and old_inv != new_inv
            if row.product_changed:
                stats.inventory_changed += 1
    log.info("product status", extra={"extra_fields": {
        "new": stats.new, "existing": stats.existing, "inventory_changed": stats.inventory_changed,
        "known": len(known)}})
    return stats
