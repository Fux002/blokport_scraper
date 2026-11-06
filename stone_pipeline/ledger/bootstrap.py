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

from stone_pipeline.config.settings import ATTRIBUTES_STATUS_REQUIRED, SETTINGS, category
from stone_pipeline.core.numbers import parse_number
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.payloads import variation_hash
from stone_pipeline.reference.medusa_files import read_attributes


def _branch_of(key: str) -> str:
    # the Key prefix is the category name; validate it against the ACTIVE PACK's registry (pack-driven), not
    # a hardcoded stone category set, so a non-stone pack's categories are recognized instead of dropped to "".
    head = key.split("_", 1)[0]
    return head if category(head) else ""


def seed_attributes(ledger: Ledger, path: str | Path | None = None) -> int:
    """Load attributes.csv (category, value, sourceid) into the `attribute` table,
    each `synced` with its Medusa id. This is the existing controlled vocabulary;
    NEW discovered values are a different path (design C3, the pull contract)."""
    path = Path(path or SETTINGS.paths.attributes_csv)
    now = now_iso()
    n = 0
    for a in read_attributes(path, require_status=ATTRIBUTES_STATUS_REQUIRED):
        ledger.upsert("attribute", {
            "category": a.category,
            "value": a.value,
            "medusa_id": a.sourceid or None,
            "state": "synced",
            "created_at": now,
            "updated_at": now,
        }, pk=("category", "value"))
        n += 1
    return n


def restore_attribute_ids(ledger: Ledger, path: str | Path | None = None) -> int:
    """Re-stamp EVERY attribute value's Medusa id (category / color / finish / quality / type) from
    attributes.csv, marking those rows `synced`. Returns the number restored.

    An attribute id is PRE-EXISTING Medusa identity (env vocab carried in from_medusa/<env>/attributes.csv),
    never a scraper-assigned sync id. A global sync reset nulls ALL of them via the attribute overlay reset,
    but populate/render reads each product's category AND color/finish/quality/type id from this table -- a
    nulled id emits a product with an empty attribute (and, for category, wedges the pull). open_ledger only
    re-seeds when the variation table is EMPTY, which a non-hard reset never leaves it, so restore here. A
    genuinely NEW value (scraped, not yet in attributes.csv) is absent from the file, so it stays `pending`
    for normal sync -- exactly as before, just no longer limited to the category rows."""
    path = Path(path or SETTINGS.paths.attributes_csv)
    if not path.exists():
        return 0
    now = now_iso()
    n = 0
    for a in read_attributes(path, require_status=ATTRIBUTES_STATUS_REQUIRED):
        if not a.sourceid:
            continue
        n += ledger.execute(
            "UPDATE attribute SET medusa_id = ?, state = 'synced', updated_at = ? "
            "WHERE category = ? AND value = ?",
            (a.sourceid, now, a.category, a.value)).rowcount
    return n


def seed_variations(ledger: Ledger, path: str | Path | None = None) -> int:
    """Load variants_export.csv (Id, Key, Name, Image, Aliases, Volume) into the
    `variation` table, each `synced` with its Medusa Id (the variation half of the
    bootstrap full load). branch is parsed from the Key; type is left blank, since
    the export does not carry the canonical stone type (the canonical-row populate
    fills it later). Dormant when the export is absent (returns 0): a factory reset
    deletes it so its stale ids can never re-hydrate ghost variations, and the cold
    start after it seeds none until Medusa publishes a fresh one."""
    path = Path(path or SETTINGS.paths.variants_export_csv)
    if not path.exists():
        return 0
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
                # the one hash of populate_variations_full (payloads.variation_hash). image_sha256 is None here
                # (the export carries no fingerprint); the first produce fills the real fingerprint from S3, which
                # re-serves any variant Medusa holds without its now-existing texture.
                "payload_hash": variation_hash({"branch": branch, "type": "", "name": name,
                                                "aliases": json.dumps(aliases), "image_url": image_url,
                                                "image_sha256": None}),
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
            # last_synced: the export shows Medusa holds it (db.in_medusa_sql), though no id was acked
            "INSERT OR IGNORE INTO product (sku, source, surrogate_key, handle, "
            "inventory_quantity, state, last_synced, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sku, src.lower(), surrogate, info.get("handle") or "", inv or None, "synced", now, now, now),
        )
        # parse via the shared numeric front door: a bare .isdigit() coerced '10.0'/'1,000'/'12 ' to 0
        # (a phantom out-of-stock that then re-serves as a spurious delta). parse_number reads them right.
        _n = parse_number(inv)
        qty = int(_n) if _n is not None and _n > 0 else 0
        # the last synced stock equals the stock so the bootstrap itself produces no spurious delta
        ledger.upsert("inventory", {"sku": sku, "qty": qty, "made_to_order": 0, "last_synced_qty": qty,
                                    "last_synced_made_to_order": 0, "updated_at": now}, pk=("sku",))
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
