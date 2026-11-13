"""Bootstrap the ledger from the current Medusa state (SYNC_LEDGER_DESIGN.md 5B).

The first sync is a full load: every owned entity starts `synced` with its real
Medusa id, so subsequent runs are deltas, not reloads. These seeders read the
from_medusa exports (the authoritative id source) and populate the ledger. They are
read-only against the exports and never write Medusa.

This is the id foundation everything id-bearing builds on: products reference
attribute ids (by name) and variation ids (by Key), which live here after the seed.
No em dashes (design principle 2).
"""

from __future__ import annotations

from pathlib import Path

from stone_pipeline.config.settings import ATTRIBUTES_STATUS_REQUIRED, SETTINGS, VARIANTS_EXPORT_STATUS_REQUIRED
from stone_pipeline.core.numbers import parse_number
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.payloads import variation_hash
from stone_pipeline.phase1.variety_key import variety_key_problem
from stone_pipeline.reference.medusa_files import CATEGORY_KIND, read_attributes, read_variants_export


def seed_attributes(ledger: Ledger, path: str | Path | None = None) -> int:
    """Load attributes.csv (category, value, sourceid) into the `attribute` table,
    each `synced` with its Medusa id. This is the existing controlled vocabulary;
    NEW discovered values are a different path (design C3, the pull contract). Category rows are skipped: a
    category is the registry's, by its exact handle (table 577)."""
    path = Path(path or SETTINGS.paths.attributes_csv)
    now = now_iso()
    n = 0
    for a in read_attributes(path, require_status=ATTRIBUTES_STATUS_REQUIRED):
        if a.category == CATEGORY_KIND:
            continue
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
    """Re-stamp EVERY attribute value's Medusa id (color / finish / quality / type) from attributes.csv, marking
    those rows `synced`. Returns the number restored. A category is not here: its pcat is the registry's
    (table 577), so a reset can never null it.

    An attribute id is PRE-EXISTING Medusa identity (env vocab carried in from_medusa/<env>/attributes.csv),
    never a scraper-assigned sync id. A global sync reset nulls ALL of them via the attribute overlay reset,
    but populate/render reads each product's color/finish/quality/type id from this table -- a
    nulled id emits a product with an empty attribute. open_ledger only
    re-seeds when the variation table is EMPTY, which a non-hard reset never leaves it, so restore here. A
    genuinely NEW value (scraped, not yet in attributes.csv) is absent from the file, so it stays `pending`
    for normal sync -- exactly as before, just no longer limited to the category rows."""
    path = Path(path or SETTINGS.paths.attributes_csv)
    if not path.exists():
        return 0
    now = now_iso()
    n = 0
    for a in read_attributes(path, require_status=ATTRIBUTES_STATUS_REQUIRED):
        if not a.sourceid or a.category == CATEGORY_KIND:   # a category is the registry's (table 577)
            continue
        n += ledger.execute(
            "UPDATE attribute SET medusa_id = ?, state = 'synced', updated_at = ? "
            "WHERE category = ? AND value = ?",
            (a.sourceid, now, a.category, a.value)).rowcount
    return n


def seed_variations(ledger: Ledger, path: str | Path | None = None) -> int:
    """Load variants_export.csv (Id, Key, Name, Type) into the `variation`
    table, each `synced` with its Medusa Id (the variation half of the bootstrap full
    load). A Key the one parser refuses raises. The export carries no icons: the first
    produce writes them from the S3 listing, which re-serves a variety once with its
    icons. Dormant when the export is absent (returns 0): a factory reset
    deletes it so its stale ids can never re-hydrate ghost variations, and the cold
    start after it seeds none until Medusa publishes a fresh one."""
    path = Path(path or SETTINGS.paths.variants_export_csv)
    if not path.exists():
        return 0
    now = now_iso()
    n = 0
    for v in read_variants_export(path, require_status=VARIANTS_EXPORT_STATUS_REQUIRED):
        if not v.key:
            continue
        if (problem := variety_key_problem(v.key)) is not None:
            raise ValueError(f"{path.name}: {v.key!r} is not a variety Key ({problem})")
        ledger.upsert("variation", {
            "key": v.key,
            "type": v.type,
            "name": v.name,
            # not computed yet (NULL, held from the serve): the export carries only the backend's hand rows (P2-7),
            # and the scraper's own alias_rows and codes come from its stores at the next produce
            "alias_rows": None,
            "codes": None,
            "medusa_id": v.id or None,
            # the one hash of populate_variations_full (payloads.variation_hash), with no icons yet (above)
            "payload_hash": variation_hash({"type": v.type, "name": v.name, "alias_rows": None, "codes": None}, []),
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
