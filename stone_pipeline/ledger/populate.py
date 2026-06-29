"""Populate the ledger from canonical rows (the write-through, design section 3).

Phase 1 keeps this OUT of the live pipeline; it is exercised by the equivalence
test, which proves that populating the ledger from canonical rows and rendering it
back reproduces emit's output. The product row stores the design's name-based
fields (ids resolved at render) plus the presentation fields the legacy import CSV
needs. No em dashes (design principle 2).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso, payload_hash
from stone_pipeline.stages.emit import _sku
from stone_pipeline.stages.emit_catalog import _COLS as _VARIANTS_FULL_COLS
from stone_pipeline.stages.product_state import inventory_for


def populate_variations_full(ledger: Ledger, path: str | Path) -> int:
    """Reflect the produced 1_variants_full.csv onto the variation table: update each
    variant's content (name, aliases, image, volume) and mark it in_full=1; insert
    any new variant as pending (no Medusa id yet). Export-only rows (junk,
    consolidated-away) are reset to in_full=0 so they never render. medusa_id, state,
    branch, type and first_seen are preserved on existing rows (the bootstrap set
    them from the export); only the produced content moves."""
    _key, _name, _image, _aliases, _volume = _VARIANTS_FULL_COLS
    now = now_iso()
    ledger.execute("UPDATE variation SET in_full = 0")
    n = 0
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            key = (r.get(_key) or "").strip()
            if not key:
                continue
            name = r.get(_name) or ""
            image_url = r.get(_image) or ""
            aliases = [a for a in (r.get(_aliases) or "").split("|") if a]
            volume = r.get(_volume) or ""
            head = key.split("_", 1)[0]
            branch = head if head in ("slab", "block", "tile") else ""
            ph = payload_hash([branch, "", name, sorted(aliases), image_url, volume])
            ledger.execute(
                "INSERT INTO variation (key, branch, type, name, aliases, image_url, "
                "image_sha256, image_model, volume, medusa_id, in_full, payload_hash, "
                "state, first_seen, last_synced, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET name = excluded.name, "
                "aliases = excluded.aliases, image_url = excluded.image_url, "
                "volume = excluded.volume, in_full = 1, payload_hash = excluded.payload_hash, "
                "updated_at = excluded.updated_at",
                (key, branch, "", name, json.dumps(aliases), image_url, None, None, volume,
                 None, ph, "pending", now, None, now, now),
            )
            n += 1
    return n


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


def populate_discontinued(ledger: Ledger, pairs: Iterable[tuple[str, str]]) -> int:
    """Mark products the supplier dropped as out of stock (qty 0), the reversible
    delist (design 5A). `pairs` are the (sku, handle) tuples product_state.discontinued
    produces. Distinct from in-stock quantity (which floors at 1), so a product can
    only reach 0 through this path. A minimal product row is created if absent (the
    dropped product is not in this run's emit), without clobbering an existing one."""
    now = now_iso()
    n = 0
    for sku, handle in pairs:
        sku = (sku or "").strip()
        if not sku:
            continue
        src, _, _ = sku.partition("-")
        ledger.execute(
            "INSERT OR IGNORE INTO product (sku, source, handle, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (sku, src.lower(), handle or "", "synced", now, now),
        )
        ledger.upsert("inventory", {"sku": sku, "qty": 0, "last_synced_qty": None,
                                    "updated_at": now}, pk=("sku",))
        n += 1
    return n
