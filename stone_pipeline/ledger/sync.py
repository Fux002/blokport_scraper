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
from datetime import datetime, timedelta, timezone

from stone_pipeline.core import logfmt
from stone_pipeline.ledger.db import Ledger, now_iso

log = logfmt.get_logger("ledger.sync")


class ServeInFlight(Exception):
    """A reset was attempted while a pull holds a lease ('syncing' rows). The caller maps it to 409."""

# After this many CONSECUTIVE failed Medusa applies, an entity is dead-lettered (state 'gap_held'):
# it stops being served (no infinite poison-pill retry) and surfaces in /status for a human. A later
# populate with changed data, or an explicit requeue, un-quarantines it.
_MAX_SYNC_ATTEMPTS = 5

# A served-but-not-yet-acked entity is leased in state 'syncing' (the in-flight guard) so two
# overlapping pulls can NEVER double-serve the same rows. If Medusa pulls but never acks (crash /
# lost response), the lease is reclaimed to 'dirty' after this many seconds and re-served.
_SYNCING_LEASE_SECONDS = 900   # 15 min -- comfortably longer than a pull->apply->ack cycle


def reap_stale_syncing(ledger: Ledger, table: str | None = None) -> int:
    """Return dead leases ('syncing' older than _SYNCING_LEASE_SECONDS) to 'dirty' so they re-serve.
    Called lazily at the head of every serve, so a crashed puller's in-flight rows recover on the
    next pull without a background job. Returns the number reclaimed."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_SYNCING_LEASE_SECONDS)).isoformat()
    now, n = now_iso(), 0
    for t in ([table] if table else ["variation", "product"]):
        cur = ledger.execute(
            f"UPDATE {t} SET state = 'dirty', updated_at = ? WHERE state = 'syncing' AND updated_at < ?",
            (now, cutoff))
        n += cur.rowcount
    return n

# type name (the URL <type>) -> (table, single-column external_id)
_ENTITY = {
    "variations": ("variation", "key"),
    "products": ("product", "sku"),
    # NB: no 'combinations' -- that lane is dormant (no write-through populates it, no ready_* serves
    # it, and the combination table lacks the sync_attempts/sync_error columns the ack fail-path uses,
    # so wiring it here would make ack('combinations','failed') raise). Combinations are CSV-rendered
    # from the export and Medusa resolves priceable tuples itself. Re-add only with a full serve lane.
}
_SERVABLE = ("pending", "dirty")
# a variation always belongs to a category (strict): branch is NOT NULL and constrained,
# so every served variation maps to one canonical category name.
_CATEGORY = {"slab": "Slabs", "block": "Blocks", "tile": "Tiles"}


def _as_number(text):
    """Stored decimals are TEXT (for exact CSV round-trip); send them as JSON numbers."""
    if text is None or str(text).strip() == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None

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

def _sub_limit(limit: int | None) -> str:
    return f" LIMIT {int(limit)}" if (limit and int(limit) > 0) else ""


# ONE source of truth for "eligible to serve", reused by the lease (ready_*) AND the non-leasing
# peek (count_ready) so the two can never diverge -- a variation is servable once produced + typed;
# a product once its variation is LIVE (synced, typed, textured). `v` is the joined variation alias.
_ELIGIBLE_VARIATION = "state IN ('pending', 'dirty') AND in_full = 1 AND type IS NOT NULL AND type != ''"
_ELIGIBLE_PRODUCT = ("p.state IN ('pending', 'dirty') AND v.state = 'synced' "
                     "AND v.type IS NOT NULL AND v.type != '' "
                     "AND v.image_url IS NOT NULL AND v.image_url != ''")


def count_ready(ledger: Ledger, type_: str) -> int:
    """Non-leasing PEEK: how many rows COULD serve right now, WITHOUT leasing them. ready() would
    mark the rows 'syncing' and consume them, so use this for convergence checks, monitoring, and
    any assertion that must not have a side effect. Same eligibility predicates as ready()."""
    if type_ in ("variations", "variation"):
        return ledger.execute("SELECT COUNT(*) n FROM variation WHERE " + _ELIGIBLE_VARIATION).fetchone()["n"]
    if type_ in ("products", "product"):
        return ledger.execute(
            "SELECT COUNT(*) n FROM product p JOIN variation v ON v.key = p.variation_key "
            "WHERE " + _ELIGIBLE_PRODUCT).fetchone()["n"]
    if type_ == "inventory":
        return len(ready_inventory(ledger))
    raise ValueError(f"unsupported sync type {type_!r}")


def ready_variations(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Variations awaiting sync (pending/dirty), in the produced set, that have a canonical type.
    An untyped variation is HELD, never served (design L1). Ordered by a stable key (design L2).

    Serving atomically LEASES the rows to 'syncing' via UPDATE...RETURNING, so two overlapping pulls
    can never double-serve the same rows (the second's subquery no longer sees them as pending/dirty).
    ack flips 'syncing'->'synced'/'dirty'; an un-acked lease is reclaimed by reap_stale_syncing."""
    reap_stale_syncing(ledger, "variation")
    rows = ledger.execute(
        "UPDATE variation SET state = 'syncing', updated_at = ? WHERE key IN ("
        "  SELECT key FROM variation WHERE " + _ELIGIBLE_VARIATION +
        "  ORDER BY created_at, key" + _sub_limit(limit) + ") "
        "RETURNING key, branch, type, name, aliases, image_url, volume, payload_hash",
        (now_iso(),)).fetchall()
    return [{
        "external_id": v["key"],
        "payload_hash": v["payload_hash"],
        "payload": {
            "category": _CATEGORY.get(v["branch"], ""),   # strict: a variation belongs to a category
            "type": v["type"],
            "name": v["name"],
            "aliases": json.loads(v["aliases"] or "[]"),
            "image_url": v["image_url"] or "",
            "volume": _as_number(v["volume"]),   # m3/kg as a number, not a string
        },
    } for v in rows]


def ready_products(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Products awaiting sync whose VARIATION is synced AND has a live variant texture.

    The texture gate is the H2 hold decision: a product never lists until its variety's
    {Key}.png exists, so it is not served with scraped photos only. variation.image_url
    is non-empty exactly when that texture is live on S3 (emit_catalog only stamps the
    link when the object exists), so the gate needs no extra S3 call.

    The variation must also be TYPED: the product inherits its category and type from the
    variation, so a synced-but-untyped variation (a bootstrap-synced export row that
    fill_variation_types could not resolve) would make the product inherit an empty type
    and list broken. Gating on `v.type` holds those products until the variety is typed.

    The payload carries ZERO Medusa ids (the review's red flag): everything is an
    external reference Medusa resolves on its side. `vendor` (the source) resolves to the
    company + sales channel; ports are derived by Medusa from `origin_country_code`;
    `image_urls` are ingestion sources Medusa copies into its own storage."""
    # atomically LEASE the eligible products to 'syncing' (in-flight guard, same as variations).
    reap_stale_syncing(ledger, "product")
    rows = ledger.execute(
        "UPDATE product SET state = 'syncing', updated_at = ? WHERE sku IN ("
        "  SELECT p.sku FROM product p JOIN variation v ON v.key = p.variation_key "
        "  WHERE " + _ELIGIBLE_PRODUCT +
        "  ORDER BY p.created_at, p.sku" + _sub_limit(limit) + ") RETURNING *",
        (now_iso(),)).fetchall()
    return [{
        "external_id": p["sku"],
        "payload_hash": p["payload_hash"],
        "payload": {
            # category + type are OWNED by the variation (identity = category/type/name); the product
            # INHERITS them via variation_external_id -- resolve identity from the variation, not here.
            # We ALSO send the NAMES, denormalized, purely for display/filtering, so Medusa never has
            # to join product -> variation just to show the stone type. They always equal the
            # variation's by construction (a product is a physical instance of its variety).
            "variation_external_id": p["variation_key"],
            "category": p["category"] or "", "type": p["type"] or "",
            "color": p["color"], "finish": p["finish"], "quality": p["quality"],
            "vendor": p["vendor"],   # agnostic company name; Medusa resolves it (kept for display + fallback)
            # company_id: the per-source Medusa company id set in :4200 (empty until pasted). Medusa
            # allocates by this id directly when present, else resolves by `vendor`. ENV-SPECIFIC:
            # a dev company id differs from prod, so :4200's config is maintained per environment.
            "company_id": p["company_id"] or "",
            "title": p["title"], "description": p["description"], "handle": p["handle"],
            "weight": p["weight"], "length": p["length"],
            "width": p["width"], "height": p["height"],
            "origin_country_code": p["origin_country_code"],   # Medusa derives ports from this
            "bundle_size": p["bundle_size"],   # under coordination: pallet model is retiring the multiplier
            "image_urls": json.loads(p["product_image_keys"] or "[]"),   # ingestion sources only
        },
    } for p in rows]


def ready_inventory(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Stock deltas to push: rows whose qty moved since the last sync, for products
    that are already synced (a product must exist in Medusa before its stock loads)."""
    rows = ledger.execute(
        "SELECT i.sku AS sku, i.qty AS qty FROM inventory i JOIN product p ON p.sku = i.sku "
        "WHERE p.state = 'synced' AND (i.last_synced_qty IS NULL OR i.last_synced_qty != i.qty) "
        "ORDER BY i.sku" + _sub_limit(limit))
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

def ack(ledger: Ledger, type_: str, external_id: str, medusa_id: str | None = None,
        status_: str = "synced", reason: str | None = None) -> int:
    """Medusa talks back for ONE entity. Returns the number of ledger rows changed (0 = the
    external_id was not found, or the ack was REFUSED as out-of-order -- see the eligibility guard).
      synced -> record the minted/matched id, mark synced, clear the failure counter+reason.
      failed -> record the reason and re-serve (dirty); after _MAX_SYNC_ATTEMPTS consecutive failures
                the entity is DEAD-LETTERED to 'gap_held' (stops serving, surfaces in /status).

    ELIGIBILITY GUARD (defense in depth): a product is only marked synced if its variation is
    already synced, and stock only if its product is synced -- so a duplicate/out-of-order ack can
    never load a product ahead of its variation (which the serve gate already prevents, but the ack
    path must not trust the caller). Inventory has no id/state; a non-failed ack means Medusa holds
    this stock (last_synced_qty = qty)."""
    now = now_iso()
    if type_ == "inventory":
        if status_ == "failed":
            return 0                                    # leaves it a delta -> re-serves
        cur = ledger.execute(                            # only for a synced product (F2)
            "UPDATE inventory SET last_synced_qty = qty, updated_at = ? WHERE sku = ? "
            "AND sku IN (SELECT sku FROM product WHERE state = 'synced')", (now, external_id))
        return cur.rowcount
    if type_ not in _ENTITY:
        raise ValueError(f"unsupported sync type {type_!r}")
    table, pk = _ENTITY[type_]
    if status_ == "failed":
        row = ledger.execute(f"SELECT sync_attempts FROM {table} WHERE {pk} = ?",
                             (external_id,)).fetchone()
        attempts = ((row["sync_attempts"] if row else 0) or 0) + 1
        state = "gap_held" if attempts >= _MAX_SYNC_ATTEMPTS else "dirty"   # dead-letter at the cap
        cur = ledger.execute(
            f"UPDATE {table} SET state = ?, sync_attempts = ?, sync_error = ?, updated_at = ? "
            f"WHERE {pk} = ?",
            (state, attempts, (reason or "apply failed")[:500], now, external_id))
        if state == "gap_held" and cur.rowcount:
            log.warning("entity dead-lettered after repeated Medusa failures",
                        extra={"extra_fields": {"type": type_, "external_id": external_id,
                                                "attempts": attempts, "reason": reason}})
        return cur.rowcount
    # synced: a PRODUCT is only marked synced if its variation is synced (the eligibility guard).
    guard = " AND variation_key IN (SELECT key FROM variation WHERE state = 'synced')" \
        if type_ == "products" else ""
    cur = ledger.execute(
        f"UPDATE {table} SET medusa_id = ?, state = 'synced', sync_attempts = 0, sync_error = NULL, "
        f"last_synced = ?, updated_at = ? WHERE {pk} = ?{guard}",
        (medusa_id, now, now, external_id))
    if cur.rowcount == 0 and type_ == "products":
        log.warning("refused a product ack ahead of its variation (out-of-order / not resolvable)",
                    extra={"extra_fields": {"sku": external_id}})
    return cur.rowcount


def ack_batch(ledger: Ledger, acks: list[dict]) -> dict[str, int]:
    """Apply a batch of acks (POST /sync/ack body). Each: {type, external_id, medusa_id, status,
    error?}. Per-ack ISOLATED: a malformed/unsupported ack is skipped and logged, the rest still
    apply -- one bad ack never drops the batch. Returns {applied, missed, skipped}: applied changed a
    row; missed = external_id not found or ack refused (out-of-order); skipped = malformed."""
    applied = missed = skipped = 0
    for a in acks:
        try:
            changed = ack(ledger, a["type"], a["external_id"], a.get("medusa_id"),
                          a.get("status", "synced"), a.get("error") or a.get("reason"))
            if changed:
                applied += 1
            else:
                missed += 1
        except Exception as exc:
            skipped += 1
            log.warning("skipped a malformed ack (batch continues)",
                        extra={"extra_fields": {"ack": str(a)[:200], "error": str(exc)}})
    return {"applied": applied, "missed": missed, "skipped": skipped}


def failures(ledger: Ledger, limit: int = 200) -> list[dict]:
    """The dead-lettered entities and WHY (external_id, attempts, last error) -- the drill-down
    behind the /status gap_held count, so an operator can see what Medusa rejected and fix it."""
    out: list[dict] = []
    for type_, table in (("variations", "variation"), ("products", "product")):
        pk = _ENTITY[type_][1]
        for r in ledger.execute(
            f"SELECT {pk} AS xid, sync_attempts, sync_error FROM {table} "
            f"WHERE state = 'gap_held' ORDER BY updated_at DESC LIMIT ?", (limit,)):
            out.append({"type": type_, "external_id": r["xid"],
                        "attempts": r["sync_attempts"], "error": r["sync_error"]})
    return out


def requeue_dead_lettered(ledger: Ledger, type_: str | None = None) -> int:
    """Recovery: reset dead-lettered ('gap_held') entities back to 'dirty' so they are re-served --
    used after the underlying Medusa issue is fixed (e.g. a transient outage that hit the cap).
    Clears the counter AND the stale error. Returns the number requeued. type_ None = variations + products."""
    tables = [_ENTITY[type_][0]] if type_ else ["variation", "product"]
    now, n = now_iso(), 0
    for table in tables:
        cur = ledger.execute(
            f"UPDATE {table} SET state = 'dirty', sync_attempts = 0, sync_error = NULL, updated_at = ? "
            f"WHERE state = 'gap_held'", (now,))
        n += cur.rowcount
    return n


def serve_in_flight(ledger: Ledger) -> bool:
    """True if a pull is mid-flight: any variation/product is leased ('syncing'). A reset MUST refuse
    while this holds, or it would race an in-flight ack and re-open the ledger drift (coordination)."""
    for t in ("variation", "product"):
        if ledger.execute(f"SELECT 1 FROM {t} WHERE state = 'syncing' LIMIT 1").fetchone():
            return True
    return False


def _reset_overlay(ledger: Ledger, table: str, now: str, where: str = "", params: tuple = ()) -> int:
    """Reset ONE table's sync overlay: state -> 'pending', drop medusa_id/last_synced/sync bookkeeping.
    Content columns (name, type, image, price...) are untouched. `where`/`params` scope the rows."""
    cols = {r["name"] for r in ledger.execute(f"PRAGMA table_info({table})")}
    sets = ["state = 'pending'", "updated_at = ?"]
    sets += [f"{c} = NULL" for c in ("medusa_id", "last_synced", "sync_error") if c in cols]
    if "sync_attempts" in cols:
        sets.append("sync_attempts = 0")
    return ledger.execute(f"UPDATE {table} SET {', '.join(sets)}{where}", (now, *params)).rowcount


def reset_sync_state(ledger: Ledger, source_codes: list[str] | None = None,
                     hard: bool = False) -> dict[str, int]:
    """CLEAN START of the sync overlay, so the ledger stops drifting from a wiped Medusa.

    soft (default): every entity -> 'pending' and all Medusa ids + sync bookkeeping dropped; inventory
      baseline cleared. Re-serves the whole catalog from zero WITHOUT re-scraping.
    hard: additionally DELETE the scraped products + inventory (the scraper's per-source output), so a
      re-scrape rebuilds them from scratch.

    INVARIANT: variation/backbone rows are NEVER deleted -- they are your base config (also held in git
    as variants_export_base + backbone_*), and re-seeding them from the live export would re-introduce
    stale synced ids (the drift we are fixing). Only their sync OVERLAY is cleared.

    `source_codes` scopes products (+ their stock) to those sources; the shared base layer
    (variations/attributes/combinations) is reset only on a GLOBAL (unscoped) reset. Returns row counts.
    """
    now, out = now_iso(), {}
    # Take the write lock up front (a no-op write) and re-check in-flight WITHIN this transaction, so a
    # concurrent pull (the sync server is a separate process) cannot lease/ack between the check and
    # the reset; with the connection's busy_timeout it waits for our commit. This is the real guard;
    # the caller's earlier check is only a fast fail. Raises ServeInFlight -> the caller maps it to 409.
    ledger.execute("UPDATE ledger_meta SET updated_at = ? WHERE id = 1", (now,))
    if serve_in_flight(ledger):
        raise ServeInFlight("a pull is in flight; refusing to reset mid-serve")

    scoped = source_codes is not None          # empty list -> scope-to-nothing, NOT a global reset
    where, params = ("", ())
    if scoped:
        marks = ",".join("?" * len(source_codes)) or "NULL"   # source IN (NULL) matches no row
        where, params = (f" WHERE source IN ({marks})", tuple(source_codes))

    # shared base layer: overlay reset only on a global reset (a per-source reset leaves it alone).
    for t in ("attribute", "variation"):
        out[t] = 0 if scoped else _reset_overlay(ledger, t, now)

    if hard:                                   # drop the scraper output; re-scrape rebuilds it
        out["inventory"] = ledger.execute(
            f"DELETE FROM inventory WHERE sku IN (SELECT sku FROM product{where})", params).rowcount \
            if scoped else ledger.execute("DELETE FROM inventory").rowcount
        out["product"] = ledger.execute(f"DELETE FROM product{where}", params).rowcount
    else:                                      # keep the rows, just re-serve them from zero
        out["product"] = _reset_overlay(ledger, "product", now, where, params)
        inv_where = " WHERE sku IN (SELECT sku FROM product%s)" % where if scoped else ""
        out["inventory"] = ledger.execute(
            f"UPDATE inventory SET last_synced_qty = NULL, updated_at = ?{inv_where}",
            (now, *params)).rowcount

    log.warning("ledger sync state RESET", extra={"extra_fields": {
        "mode": "hard" if hard else "soft", "sources": source_codes or "all", "reset": out}})
    return out


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
