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
from stone_pipeline.core.text import match_key
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
                # a content change re-serves (synced -> dirty); unchanged keeps its state. medusa_id
                # and first_seen are never touched, so an acked id survives a re-run.
                "state = CASE WHEN variation.payload_hash != excluded.payload_hash "
                "THEN 'dirty' ELSE variation.state END, "
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


def fill_variation_types(ledger: Ledger) -> int:
    """Fill the canonical stone `type` name on variations that lack it, recovered from
    the Key's type slug ({branch}_{slug(type)}_{slug(name)}_{uuid}) matched against the
    seeded `type` attribute vocabulary, longest match first (the same recovery
    tree_build uses). This is the variation payload's last empty field (sync prereq).
    Returns the number filled."""
    # Key on match_key (the shared normalizer) so a Key slug (semi_precious_stone) matches a
    # hyphenated vocab value (Semi-Precious Stone); a separator/case/accent never forks the type.
    types = {match_key(r["value"]): r["value"]
             for r in ledger.execute("SELECT value FROM attribute WHERE category = 'type'")}
    if not types:
        return 0
    now = now_iso()
    n = 0
    for v in ledger.execute("SELECT key FROM variation WHERE type IS NULL OR type = ''"):
        parts = v["key"].split("_")[1:-1]   # drop the branch prefix and the trailing uuid
        found = None
        for i in range(len(parts), 0, -1):
            cand = match_key("_".join(parts[:i]))   # normalize the slug the same way as the vocab
            if cand in types:
                found = types[cand]
                break
        if found:
            ledger.execute("UPDATE variation SET type = ?, updated_at = ? WHERE key = ?",
                           (found, now, v["key"]))
            n += 1
    return n


def populate_products(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> int:
    """Write canonical rows into the `product` table, in input order. New entities
    land `pending`; the equivalence test renders them straight back."""
    now = now_iso()
    n = 0
    for r in rows:
        sku = _sku(r, cfg)
        prev = ledger.get("product", "sku", sku)
        # the product's variation KEY is stable once resolved (a SKU is always the same variety).
        # _variation_key_for maps the scrape's Medusa variation_id -> Key via variation.medusa_id,
        # which CHURNS (export id -> new ack id after a clean-start re-sync). So resolve, but fall
        # back to the already-stored Key rather than stomping a good link with NULL on a re-populate.
        variation_key = _variation_key_for(ledger, r.variation_id) or (prev["variation_key"] if prev else None)
        record = {
            "sku": sku,
            "source": cfg.source_code,
            "vendor": cfg.vendor or cfg.source,   # the company this source belongs to
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
            "company_id": r.company_id,   # already = source_cfg.company_id or the default (constants.py)
            "sales_channel_id": r.sales_channel_id,
            "ports": json.dumps(r.port_ids or []),
            "visibility": r.visibility,
            "discountable": r.discountable,
            "sold_in_bundle": 1 if r.sold_in_bundle else 0,
            "bundle_size": r.bundle_size,
            "inventory_quantity": inventory_for(r),
            "payload_hash": (ph := payload_hash([
                variation_key, r.color_name, r.finish_name, r.quality_name, r.type_name,
                r.title, r.description, r.handle, r.weight, r.length, r.width, r.height,
                r.origin_country_code, json.dumps(r.product_image_keys or []),
                r.company_id, r.sales_channel_id, _category_name_for(ledger, r.category_pcat_id),
                r.bundle_size, json.dumps(r.port_ids or []),
            ])),
            "created_at": now,
            "updated_at": now,
        }
        # Convergent sync state (design db.py header): NEW -> pending, CHANGED -> dirty, UNCHANGED
        # -> untouched. CRITICAL: preserve a synced product's medusa_id + state on re-run. The old
        # code hardcoded state='pending', medusa_id=None, so every write-through run reset the whole
        # catalog to pending and wiped the acked Medusa ids -> Medusa re-ingested everything nightly.
        if prev is None:                                     # `prev` fetched above (variation_key fallback)
            record.update(medusa_id=None, state="pending", last_synced=None)
        else:
            record.update(medusa_id=prev["medusa_id"], last_synced=prev["last_synced"],
                          state="dirty" if prev["payload_hash"] != ph else prev["state"])
        ledger.upsert("product", record, pk=("sku",), keep_on_update=("created_at", "first_seen"))
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
        # keep last_synced_qty on update: it is owned by the ack, not the write-through.
        # Overwriting it to NULL each run would re-serve every product as a delta forever.
        ledger.upsert("inventory", {
            "sku": sku,
            "qty": qty,
            "last_synced_qty": None,   # only on insert (a never-synced row is a delta)
            "updated_at": now,
        }, pk=("sku",), keep_on_update=("last_synced_qty",))
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
        # keep last_synced_qty on update (ack-owned), so a still-discontinued product
        # is not re-served as a delist on every run (same fix as populate_inventory).
        ledger.upsert("inventory", {"sku": sku, "qty": 0, "last_synced_qty": None,
                                    "updated_at": now}, pk=("sku",),
                      keep_on_update=("last_synced_qty",))
        n += 1
    return n
