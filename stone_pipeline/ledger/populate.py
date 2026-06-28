"""Populate the ledger from canonical rows (the write-through, design section 3).

Phase 1 keeps this OUT of the live pipeline; it is exercised by the equivalence
test, which proves that populating the ledger from canonical rows and rendering it
back reproduces emit's output. The product row stores the design's name-based
fields (ids resolved at render) plus the presentation fields the legacy import CSV
needs. No em dashes (design principle 2).
"""

from __future__ import annotations

import json
from typing import Iterable

from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso, payload_hash
from stone_pipeline.stages.emit import _sku
from stone_pipeline.stages.product_state import inventory_for


def _variation_key_for(ledger: Ledger, variation_id: str | None) -> str | None:
    """The product references its variation by Key; the canonical row carries the
    Medusa variation_id, so map it back through the seeded variation table."""
    if not variation_id:
        return None
    row = ledger.execute(
        "SELECT key FROM variation WHERE medusa_id = ? LIMIT 1", (variation_id,)
    ).fetchone()
    return row["key"] if row else None


def _category_name_for(ledger: Ledger, pcat_id: str | None) -> str | None:
    """Store the category by canonical NAME (re-seed safe); the row carries the
    pcat id, so reverse-map it through the seeded attribute table."""
    if not pcat_id:
        return None
    row = ledger.execute(
        "SELECT value FROM attribute WHERE category = 'category' AND medusa_id = ? LIMIT 1",
        (pcat_id,),
    ).fetchone()
    return row["value"] if row else None


def populate_products(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> int:
    """Write canonical rows into the `product` table, in input order. New entities
    land `pending`; the equivalence test renders them straight back."""
    now = now_iso()
    n = 0
    for r in rows:
        sku = _sku(r, cfg)
        variation_key = _variation_key_for(ledger, r.variation_id)
        record = {
            "sku": sku,
            "source": cfg.source_code,
            "surrogate_key": r.surrogate_key,
            "variation_key": variation_key,
            "color": r.color_name,
            "finish": r.finish_name,
            "quality": r.quality_name,
            "type": r.type_name,
            "category": _category_name_for(ledger, r.category_pcat_id),
            "title": r.title,
            "description": r.description,
            "handle": r.handle,
            "slug": r.slug,
            "weight": r.weight,
            "length": r.length,
            "width": r.width,
            "height": r.height,
            "origin_country_code": r.origin_country_code,
            "origin_city": r.origin_city,
            "origin_county": r.origin_county,
            "thumbnail_key": r.thumbnail_key,
            "oriented_image_keys": json.dumps(r.oriented_image_keys or []),
            "product_image_keys": json.dumps(r.product_image_keys or []),
            "company_id": r.company_id,
            "sales_channel_id": r.sales_channel_id,
            "ports": json.dumps(r.port_ids or []),
            "visibility": r.visibility,
            "discountable": r.discountable,
            "sold_in_bundle": 1 if r.sold_in_bundle else 0,
            "bundle_size": r.bundle_size,
            "inventory_quantity": inventory_for(r),
            "medusa_id": None,
            "payload_hash": payload_hash([
                variation_key, r.color_name, r.finish_name, r.quality_name, r.type_name,
                r.title, r.description, r.handle, r.weight, r.length, r.width, r.height,
                r.origin_country_code, json.dumps(r.product_image_keys or []),
                r.company_id, r.sales_channel_id, _category_name_for(ledger, r.category_pcat_id),
                r.bundle_size, json.dumps(r.port_ids or []),
            ]),
            "state": "pending",
            "last_synced": None,
            "created_at": now,
            "updated_at": now,
        }
        ledger.upsert("product", record, pk=("sku",))
        n += 1
    return n


def populate_inventory(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> int:
    """Write per-product stock into the `inventory` table (qty = emit.inventory_for).
    `last_synced_qty` is left null, so a freshly populated row is a delta to serve
    (design section 7). Products must be populated first (the sku FK)."""
    now = now_iso()
    n = 0
    for r in rows:
        sku = _sku(r, cfg)
        resolved = inventory_for(r)
        qty = int(resolved) if str(resolved).isdigit() else 0
        ledger.upsert("inventory", {
            "sku": sku,
            "qty": qty,
            "last_synced_qty": None,
            "updated_at": now,
        }, pk=("sku",))
        n += 1
    return n


def populate_discontinued(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> int:
    """Mark products the supplier dropped as out of stock (qty 0), the reversible
    delist (design 5A). This is distinct from in-stock quantity, which floors at 1,
    so a discontinued product can only become 0 through this path."""
    now = now_iso()
    n = 0
    for r in rows:
        ledger.upsert("inventory", {
            "sku": _sku(r, cfg),
            "qty": 0,
            "last_synced_qty": None,
            "updated_at": now,
        }, pk=("sku",))
        n += 1
    return n
