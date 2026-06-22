"""Stage 10: emit (section 7 Stage 10, operating principle 8).

Read the live template to get the exact column list and order; map each canonical
row onto those columns. The template is the schema authority, so columns are never
hardcoded here, only the mapping from a column name to the canonical value that
fills it. Booleans emit lowercase (true/false) to match the real upload file, not
an assumed casing. Writes the import CSV plus the review and reject artifacts.

A later milestone swaps this CSV writer for a direct Medusa API client with upsert
semantics; nothing upstream changes.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow

log = logfmt.get_logger("emit")


def _num(value) -> str:
    if value is None:
        return ""
    return f"{value:g}" if isinstance(value, float) else str(value)


def _bool(value) -> str:
    return "true" if value else "false"


def _sku(row: CanonicalRow, cfg: SourceConfig) -> str:
    return f"{cfg.source_code}-{row.surrogate_key}".upper()


def _inventory(row: CanonicalRow) -> str:
    for candidate in (row.raw_slab_count, row.bundle_size):
        if candidate and str(candidate).strip().isdigit():
            return str(int(str(candidate)))
    return "1"


def _slot(keys: list[str], index: int) -> str:
    return keys[index] if index < len(keys) else ""


# column name -> (row, cfg) -> cell string. Driven by the live template order.
COLUMN_MAP: dict[str, Callable[[CanonicalRow, SourceConfig], str]] = {
    "Product Handle": lambda r, c: r.handle or "",
    "Product Title": lambda r, c: r.title or "",
    "Product Status": lambda r, c: SETTINGS.backend.status,
    "Product Description": lambda r, c: r.description or "",
    "Product Weight": lambda r, c: _num(r.weight),
    "Product Length": lambda r, c: _num(r.length),
    "Product Width": lambda r, c: _num(r.width),
    "Product Height": lambda r, c: _num(r.height),
    "Product Discountable": lambda r, c: r.discountable or SETTINGS.backend.discountable,
    "Product Thumbnail": lambda r, c: r.thumbnail_key or "",
    "Product Category 1": lambda r, c: r.category_pcat_id or "",
    "Product Sales Channel 1": lambda r, c: r.sales_channel_id or "",
    "Front Image": lambda r, c: _slot(r.oriented_image_keys, 0),
    "Right Image": lambda r, c: _slot(r.oriented_image_keys, 1),
    "Back Image": lambda r, c: _slot(r.oriented_image_keys, 2),
    "Left Image": lambda r, c: _slot(r.oriented_image_keys, 3),
    "Product Image 1": lambda r, c: _slot(r.product_image_keys, 0),
    "Product Image 2": lambda r, c: _slot(r.product_image_keys, 1),
    "Product Image 3": lambda r, c: _slot(r.product_image_keys, 2),
    "Product Image 4": lambda r, c: _slot(r.product_image_keys, 3),
    "Product Image 5": lambda r, c: _slot(r.product_image_keys, 4),
    "Variant Title": lambda r, c: SETTINGS.backend.variant_title,
    "Variant Sku": _sku,
    "Variant Manage Inventory": lambda r, c: SETTINGS.backend.variant_manage_inventory,
    "Variant Allow Backorder": lambda r, c: SETTINGS.backend.variant_allow_backorder,
    "Variant Option 1 Name": lambda r, c: SETTINGS.backend.variant_option_1_name,
    "Variant Option 1 Value": lambda r, c: SETTINGS.backend.variant_option_1_value,
    "Variant Inventory Quantity": lambda r, c: _inventory(r),
    "STN Origin Country Code": lambda r, c: r.origin_country_code or "",
    "STN Origin City": lambda r, c: r.origin_city or "",
    "STN Origin County": lambda r, c: r.origin_county or "",
    "STN Color Id": lambda r, c: r.color_id or "",
    "STN Finish Id": lambda r, c: r.finish_id or "",
    "STN Quality Id": lambda r, c: r.quality_id or "",
    "STN Variation Id": lambda r, c: r.variation_id or "",
    "STN Type Id": lambda r, c: r.type_id or "",
    "STN Type Name": lambda r, c: r.type_name or "",
    "STN Company Id": lambda r, c: r.company_id or "",
    "STN Port Ids": lambda r, c: "|".join(r.port_ids or []),
    "STN Visibility": lambda r, c: r.visibility or SETTINGS.backend.visibility,
    "STN Slug": lambda r, c: r.slug or "",
    "STN Popular": lambda r, c: "false",
    "STN Sold In Bundle": lambda r, c: _bool(r.sold_in_bundle),
    "STN Bundle Size": lambda r, c: _num(r.bundle_size),
    "STN Specification File Ids": lambda r, c: "",
}


def read_template_columns(path: Path | None = None) -> list[str]:
    path = Path(path or SETTINGS.paths.template_csv)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return next(csv.reader(handle))


def row_to_cells(row: CanonicalRow, columns: list[str], cfg: SourceConfig) -> dict[str, str]:
    cells: dict[str, str] = {}
    for col in columns:
        mapper = COLUMN_MAP.get(col)
        cells[col] = mapper(row, cfg) if mapper else ""
    return cells


def write_import_csv(rows: list[CanonicalRow], cfg: SourceConfig, path: Path,
                     columns: list[str] | None = None) -> Path:
    columns = columns or read_template_columns()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row_to_cells(row, columns, cfg))
    log.info("emit done", extra={"extra_fields": {"rows": len(rows), "columns": len(columns)}})
    return path


def write_review_csv(rows: list[CanonicalRow], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["src_site", "surrogate_key", "raw_name", "field", "code", "raw_value",
               "best_guess", "confidence", "method", "src_url"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            for flag in row.review_flags:
                writer.writerow({
                    "src_site": row.src_site, "surrogate_key": row.surrogate_key,
                    "raw_name": row.raw_name, "field": flag.field, "code": flag.code,
                    "raw_value": flag.raw_value, "best_guess": flag.best_guess,
                    "confidence": flag.confidence, "method": flag.method, "src_url": flag.src_url,
                })
    return path


# The Medusa inventory importer reads only `Variant Sku` and `Inventory Quantity`,
# but expects the full column structure present.
INVENTORY_COLUMNS = ["Variant Sku", "Product Handle", "Variant Title",
                     "Inventory Quantity", "Reserved Quantity"]


def write_inventory_csv(rows: list[CanonicalRow], cfg: SourceConfig, path: Path) -> Path:
    """Inventory-only update (item 5): for existing products whose stock changed,
    emit the inventory CSV. The importer uses only Variant Sku (which product) and
    Inventory Quantity, but the full structure is written so the file loads."""
    from stone_pipeline.stages.product_state import inventory_for, sku_for

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INVENTORY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "Variant Sku": sku_for(row, cfg),          # read by importer
                "Product Handle": row.handle or "",
                "Variant Title": SETTINGS.backend.variant_title,
                "Inventory Quantity": inventory_for(row),  # read by importer
                "Reserved Quantity": "",
            })
    return path


def write_rejects_csv(rows: list[CanonicalRow], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = ["src_site", "surrogate_key", "raw_name", "rule", "detail", "src_url"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            for reason in row.reject_reasons:
                writer.writerow({
                    "src_site": row.src_site, "surrogate_key": row.surrogate_key,
                    "raw_name": row.raw_name, "rule": reason.rule, "detail": reason.detail,
                    "src_url": row.src_url,
                })
    return path
