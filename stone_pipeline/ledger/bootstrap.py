"""Bootstrap the ledger from the current Medusa state (SYNC_LEDGER_DESIGN.md 5B).

The first sync is a full load: every owned entity starts `synced` with its real
Medusa id, so subsequent runs are deltas, not reloads. These seeders read the
from_medusa exports (the authoritative id source) and populate the ledger. They are
read-only against the exports and never write Medusa.

This is the id foundation everything id-bearing builds on: products and
combinations reference attribute ids (by name) and variation ids (by Key), which
live here after the seed. No em dashes (design principle 2).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.db import Ledger, now_iso, payload_hash


def _branch_of(key: str) -> str:
    head = key.split("_", 1)[0]
    return head if head in ("slab", "block", "tile") else ""


def seed_attributes(ledger: Ledger, path: str | Path | None = None) -> int:
    """Load attributes.csv (category, value, sourceid) into the `attribute` table,
    each `synced` with its Medusa id. This is the existing controlled vocabulary;
    NEW discovered values are a different path (design C3, the pull contract)."""
    path = Path(path or SETTINGS.paths.attributes_csv)
    now = now_iso()
    n = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            category = (r.get("category") or "").strip()
            value = (r.get("value") or "").strip()
            sourceid = (r.get("sourceid") or "").strip()
            if not category or not value:
                continue
            ledger.upsert("attribute", {
                "category": category,
                "value": value,
                "medusa_id": sourceid or None,
                "state": "synced",
                "created_at": now,
                "updated_at": now,
            }, pk=("category", "value"))
            n += 1
    return n


def seed_variations(ledger: Ledger, path: str | Path | None = None) -> int:
    """Load variants_export.csv (Id, Key, Name, Image, Aliases, Volume) into the
    `variation` table, each `synced` with its Medusa Id (the variation half of the
    bootstrap full load). branch is parsed from the Key; type is left blank, since
    the export does not carry the canonical stone type (the canonical-row populate
    fills it later)."""
    path = Path(path or SETTINGS.paths.variants_export_csv)
    now = now_iso()
    n = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            key = (r.get("Key") or "").strip()
            if not key:
                continue
            medusa_id = (r.get("Id") or "").strip()
            name = r.get("Name") or ""
            aliases = [a for a in (r.get("Aliases") or "").split("|") if a]
            image_url = r.get("Image") or ""
            volume = r.get("Volume per kg (m³/kg)") or ""
            branch = _branch_of(key)
            ledger.upsert("variation", {
                "key": key,
                "branch": branch,
                "type": "",
                "name": name,
                "aliases": json.dumps(aliases),
                "image_url": image_url,
                "image_sha256": None,
                "image_model": None,
                "volume": volume,
                "medusa_id": medusa_id or None,
                "payload_hash": payload_hash([branch, "", name, sorted(aliases), image_url, volume]),
                "state": "synced",
                "first_seen": now,
                "last_synced": now,
                "created_at": now,
                "updated_at": now,
            }, pk=("key",))
            n += 1
    return n


def seed_products(ledger: Ledger, path: str | Path | None = None) -> int:
    """Seed minimal product rows for every known Medusa product (products_export:
    SKU, Handle, Inventory), so inventory deltas and discontinued delists have their
    FK rows (design 5B / M1). These carry NO variation_key, so they never render into
    a product import CSV; a real run later enriches the row with the full data. Dormant
    when products_export is absent (returns 0). INSERT OR IGNORE so an already-enriched
    row from a run is never clobbered."""
    from stone_pipeline.stages.product_state import load_known_products

    known = load_known_products(Path(path) if path else None)
    now = now_iso()
    n = 0
    for sku, info in known.by_sku.items():
        src, _, surrogate = sku.partition("-")
        inv = (info.get("inventory") or "").strip()
        ledger.execute(
            "INSERT OR IGNORE INTO product (sku, source, surrogate_key, handle, "
            "inventory_quantity, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sku, src.lower(), surrogate, info.get("handle") or "", inv or None, "synced", now, now),
        )
        qty = int(inv) if inv.isdigit() else 0
        # last_synced_qty equals qty so the bootstrap itself produces no spurious delta
        ledger.upsert("inventory", {"sku": sku, "qty": qty, "last_synced_qty": qty,
                                    "updated_at": now}, pk=("sku",))
        n += 1
    return n


def attribute_id(ledger: Ledger, category: str, value: str) -> str | None:
    """Resolve a canonical attribute name to its Medusa id (the render-time lookup
    products and combinations use). Returns None if the attribute is not seeded (the query applies no
    state filter -- attributes are never moved out of 'synced', so a seeded id is always resolvable)."""
    row = ledger.execute(
        "SELECT medusa_id FROM attribute WHERE category = ? AND value = ?",
        (category, value),
    ).fetchone()
    return row["medusa_id"] if row else None
