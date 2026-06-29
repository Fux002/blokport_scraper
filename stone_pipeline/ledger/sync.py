"""Sync service over the ledger (SYNC_LEDGER_DESIGN.md section 8), scraper side.

The bidirectional sync loop, transport-free (an HTTP layer is a thin wrapper):

    Medusa pull job                          scraper sync engine (this module)
    ---------------                          ---------------------------------
    GET  /sync/<type>?status=ready   ----->  ready(ledger, type)   serve eligible entities
    (apply each in Medusa)
    POST /sync/ack {external_id, id} ----->  ack(ledger, ...)      Medusa talks back: record the
                                                                   minted id, mark the entity synced
    GET  /sync/status                ----->  status(ledger)        per-type / per-state summary

`ack` is what keeps the two systems in sync: an entity is served while it is
pending/dirty, and once Medusa acks the id it minted, the entity flips to synced and
is no longer served. Re-running is therefore convergent: only the un-synced delta
ever moves.

Eligibility (8.4) is enforced server-side so the puller cannot load out of order: a
product is served only once its variation is synced AND that variety's texture is
live (the H2 hold, via variation.image_url), and stock only once its product is
synced. One gate is still deferred: every referenced attribute synced (all
attributes are synced after bootstrap; NEW values are the dormant C3 case). No em
dashes (design principle 2).
"""

from __future__ import annotations

import json

from stone_pipeline.ledger.db import Ledger, now_iso

# type name (the URL <type>) -> (table, single-column external_id)
_ENTITY = {
    "variations": ("variation", "key"),
    "products": ("product", "sku"),
    "combinations": ("combination", "combo_key"),
}
_SERVABLE = ("pending", "dirty")

# tables that carry a sync `state` column (combination is empty until materialized)
_STATE_TABLES = ("attribute", "variation", "combination", "product", "gap")


# --- GET /sync/status ---------------------------------------------------------

def status(ledger: Ledger) -> dict[str, dict[str, int]]:
    """Per-type state counts. State-bearing tables report their state histogram;
    inventory reports total rows and the `delta` count (stock that moved since the
    last sync), which is what the inventory lane would serve."""
    out: dict[str, dict[str, int]] = {t: ledger.counts(t) for t in _STATE_TABLES}
    inv = ledger.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN last_synced_qty IS NULL OR last_synced_qty != qty THEN 1 ELSE 0 END) AS delta "
        "FROM inventory"
    ).fetchone()
    out["inventory"] = {"total": inv["total"] or 0, "delta": inv["delta"] or 0}
    return out


# --- GET /sync/<type>?status=ready --------------------------------------------

def _limit(sql: str, limit: int | None) -> str:
    return sql + (f" LIMIT {int(limit)}" if limit else "")


def ready_variations(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Variations awaiting sync (pending/dirty), in the produced set, that have a
    canonical type. An untyped variation is HELD, never served: Medusa could not
    resolve its type and it would list broken (the autonomy boundary, design L1). The
    pipeline already surfaces those for re-typing. Ordered by a stable key so paging
    is repeatable (design L2)."""
    rows = ledger.execute(_limit(
        "SELECT key, branch, type, name, aliases, image_url, volume, payload_hash "
        "FROM variation WHERE state IN ('pending', 'dirty') AND in_full = 1 "
        "AND type IS NOT NULL AND type != '' "
        "ORDER BY created_at, key", limit))
    return [{
        "external_id": v["key"],
        "payload_hash": v["payload_hash"],
        "payload": {
            "branch": v["branch"],
            "type": v["type"],
            "name": v["name"],
            "aliases": json.loads(v["aliases"] or "[]"),
            "image_url": v["image_url"] or "",
            "volume": v["volume"] or "",
        },
    } for v in rows]


def ready_products(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Products awaiting sync whose VARIATION is synced AND has a live variant texture.

    The texture gate is the H2 hold decision: a product never lists until its variety's
    {Key}.png exists, so it is not served with scraped photos only. variation.image_url
    is non-empty exactly when that texture is live on S3 (emit_catalog only stamps the
    link when the object exists), so the gate needs no extra S3 call. The attribute-synced
    gate is still deferred (all attributes are synced after bootstrap; see the docstring)."""
    rows = ledger.execute(_limit(
        "SELECT p.* FROM product p JOIN variation v ON v.key = p.variation_key "
        "WHERE p.state IN ('pending', 'dirty') AND v.state = 'synced' "
        "AND v.image_url IS NOT NULL AND v.image_url != '' "
        "ORDER BY p.created_at, p.sku", limit))
    return [{
        "external_id": p["sku"],
        "payload_hash": p["payload_hash"],
        "payload": {
            "variation_external_id": p["variation_key"],
            "color": p["color"], "finish": p["finish"], "quality": p["quality"],
            "type": p["type"], "category": p["category"],
            "title": p["title"], "description": p["description"], "handle": p["handle"],
            "weight": p["weight"], "length": p["length"],
            "width": p["width"], "height": p["height"],
            "origin_country_code": p["origin_country_code"],
            "company_id": p["company_id"], "sales_channel_id": p["sales_channel_id"],
            "bundle_size": p["bundle_size"],
            "ports": json.loads(p["ports"] or "[]"),
            "image_urls": json.loads(p["product_image_keys"] or "[]"),
        },
    } for p in rows]


def ready_inventory(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Stock deltas to push: rows whose qty moved since the last sync, for products
    that are already synced (a product must exist in Medusa before its stock loads)."""
    rows = ledger.execute(_limit(
        "SELECT i.sku AS sku, i.qty AS qty FROM inventory i JOIN product p ON p.sku = i.sku "
        "WHERE p.state = 'synced' AND (i.last_synced_qty IS NULL OR i.last_synced_qty != i.qty) "
        "ORDER BY i.sku", limit))
    return [{"external_id": r["sku"], "payload": {"sku": r["sku"], "quantity": r["qty"]}}
            for r in rows]


def ready(ledger: Ledger, type_: str, limit: int | None = None) -> list[dict]:
    """Serve the entities of `type_` that are eligible to load now (GET /sync/<type>)."""
    if type_ == "variations":
        return ready_variations(ledger, limit)
    if type_ == "products":
        return ready_products(ledger, limit)
    if type_ == "inventory":
        return ready_inventory(ledger, limit)
    raise ValueError(f"unsupported sync type {type_!r}; expected variations, products or inventory")


# --- POST /sync/ack -----------------------------------------------------------

def ack(ledger: Ledger, type_: str, external_id: str,
        medusa_id: str | None = None, status_: str = "synced") -> None:
    """Medusa talks back: record the id it minted or matched for one entity and mark
    it synced, so it is no longer served. A `failed` ack returns the entity to dirty
    (the next pull re-offers it). This is the write-back that keeps the two in sync.

    Inventory is special: it has no id or state, so a successful ack means Medusa now
    holds this stock level (last_synced_qty = qty), and the row stops being a delta."""
    now = now_iso()
    if type_ == "inventory":
        if status_ != "failed":
            ledger.execute(
                "UPDATE inventory SET last_synced_qty = qty, updated_at = ? WHERE sku = ?",
                (now, external_id))
        return
    if type_ not in _ENTITY:
        raise ValueError(f"unsupported sync type {type_!r}")
    table, pk = _ENTITY[type_]
    if status_ == "failed":
        ledger.execute(f"UPDATE {table} SET state = 'dirty', updated_at = ? WHERE {pk} = ?",
                       (now, external_id))
        return
    ledger.execute(
        f"UPDATE {table} SET medusa_id = ?, state = 'synced', last_synced = ?, updated_at = ? "
        f"WHERE {pk} = ?",
        (medusa_id, now, now, external_id))


def ack_batch(ledger: Ledger, acks: list[dict]) -> int:
    """Apply a batch of acks (POST /sync/ack body). Each: {type, external_id,
    medusa_id, status}. Returns the count applied."""
    n = 0
    for a in acks:
        ack(ledger, a["type"], a["external_id"], a.get("medusa_id"), a.get("status", "synced"))
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    import json as _json

    from stone_pipeline.ledger import writethrough

    path = writethrough.ledger_path()
    if not path.exists():
        print(f"no ledger at {path} (run with BLOKPORT_LEDGER_WRITETHROUGH=1 first)")
        return 1
    with Ledger.open(path, env=writethrough.ENV_NAME) as ledger:
        print(_json.dumps(status(ledger), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
