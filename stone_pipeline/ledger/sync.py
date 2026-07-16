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

from stone_pipeline.config.settings import CATEGORIES
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
# a variation always belongs to a category (strict): branch is NOT NULL and constrained, so every served
# variation maps to one canonical category label. Derived from the pack-built category registry (was
# hardcoded name->label).
_CATEGORY = {c.name: c.label for c in CATEGORIES}


def _as_number(text):
    """Stored decimals are TEXT (for exact CSV round-trip); send them as JSON numbers."""
    if text is None or str(text).strip() == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None

# tables that carry a sync `state` column (combination is empty until materialized)
_STATE_TABLES = ("attribute", "variation", "combination", "product", "gap", "removed")


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
    # E13: variation reconcile signal. `retiring` already surfaces in out["variation"] (a state count);
    # `orphaned` = synced, rendered varieties with 0 products (a source removal's shared orphan, or the old
    # side of a re-key awaiting retire). A distinct bucket, NOT a state, so it never double-counts the
    # histogram -- the operator can drive it toward zero by retiring the dead ones.
    orphaned = ledger.execute(
        "SELECT COUNT(*) AS n FROM variation v WHERE v.state = 'synced' AND v.in_full = 1 "
        "AND NOT EXISTS (SELECT 1 FROM product p WHERE p.variation_key = v.key)").fetchone()
    out["variation_reconcile"] = {"orphaned": orphaned["n"] or 0}
    return out


def gate_state(ledger: Ledger) -> tuple[int, int]:
    """(held, untyped) for the produce's pull-model catalog-gate reconciliation. The ledger owns these
    schema queries; produce owns only the decision made from them:
      held     produced variations with NO Medusa id yet -- awaiting the first pull (the ack assigns
               medusa_id + flips them synced). EXPECTED, not an error.
      untyped  a SUBSET of held still lacking a canonical type: the sync engine holds these until typed
               (never serves an untyped variation), so it is informational, NOT fatal.
    (No `dangling` count: a product's variation_key is a FK to variation.key with foreign_keys=ON, so a
    product with no variation is structurally impossible -- it can never exist to be a fatal fault.)"""
    def n(sql: str) -> int:
        return ledger.execute(sql).fetchone()["n"]
    new_pending = "in_full = 1 AND medusa_id IS NULL AND state IN ('pending', 'dirty')"
    return (
        n(f"SELECT COUNT(*) n FROM variation WHERE {new_pending}"),
        n(f"SELECT COUNT(*) n FROM variation WHERE {new_pending} AND (type IS NULL OR type = '')"),
    )


def typing_health(ledger: Ledger) -> tuple[int, int, int]:
    """(vocab, produced, typed) for the cold-start stall guard. Typing is a HARD serve gate (an untyped
    variety never serves, and neither do its products), so a produce that typed NOTHING has silently
    stalled the whole catalogue:
      vocab     seeded `type` attribute values (from the Medusa attributes export). 0 -> nothing types.
      produced  variations in this produce's full set (in_full = 1).
      typed     of those, how many carry a canonical type.
    typed == 0 with produced > 0 (or vocab == 0) is a broken cold start, NOT the expected pass-1 hold."""
    def n(sql: str) -> int:
        return ledger.execute(sql).fetchone()["n"]
    return (
        n("SELECT COUNT(*) n FROM attribute WHERE category = 'type'"),
        n("SELECT COUNT(*) n FROM variation WHERE in_full = 1"),
        n("SELECT COUNT(*) n FROM variation WHERE in_full = 1 AND type IS NOT NULL AND type != ''"),
    )


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


def _isolate(ledger: Ledger, table: str, pk_col: str, rows, build) -> list[dict]:
    """Build each LEASED row's serve dict, isolating one whose stored JSON/cell is corrupt: it is
    dead-lettered to 'gap_held' (so it stops re-serving and surfaces in /failures) and dropped from THIS
    page, instead of raising and 500-ing the whole leased delta. One bad row can never strand the pull."""
    out: list[dict] = []
    for row in rows:
        try:
            out.append(build(row))
        except Exception as exc:
            pk = row[pk_col]
            log.exception("ledger serve: row has an unserializable cell; dead-lettering it",
                          extra={"extra_fields": {"table": table, "pk": pk, "error": str(exc)}})
            ledger.execute(f"UPDATE {table} SET state = 'gap_held', sync_error = ?, updated_at = ? "
                           f"WHERE {pk_col} = ?", (f"serve: {exc}"[:500], now_iso(), pk))
    return out


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
    return _isolate(ledger, "variation", "key", rows, lambda v: {
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
    })


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
    return _isolate(ledger, "product", "sku", rows, lambda p: {
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
            # Images -- the SAME set the working CSV import carried, so Medusa builds the same media:
            #   thumbnail      = the CSV 'Product Thumbnail' (the product's MAIN display image)
            #   oriented_images= the CSV 'Front/Right/Back/Left Image' (a block's oriented shots)
            #   image_urls     = the CSV 'Product Image 1..N' (the gallery)
            # All are S3 ingestion sources Medusa copies into its own storage. The variety's texture
            # ({Key}.png) rides on the VARIATION payload separately.
            "thumbnail": p["thumbnail_key"] or "",
            "oriented_images": json.loads(p["oriented_image_keys"] or "[]"),
            "image_urls": json.loads(p["product_image_keys"] or "[]"),
        },
    })


def ready_inventory(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Stock deltas to push: rows whose qty moved since the last sync, for products
    that are already synced (a product must exist in Medusa before its stock loads)."""
    rows = ledger.execute(
        "SELECT i.sku AS sku, i.qty AS qty, i.reason AS reason FROM inventory i JOIN product p ON p.sku = i.sku "
        "WHERE p.state = 'synced' AND (i.last_synced_qty IS NULL OR i.last_synced_qty != i.qty) "
        "ORDER BY i.sku" + _sub_limit(limit))
    out = []
    for r in rows:
        payload = {"sku": r["sku"], "quantity": r["qty"]}
        if r["reason"]:                                  # 'delisted' -> Medusa hides; absent -> plain out-of-stock
            payload["reason"] = r["reason"]
        out.append({"external_id": r["sku"], "payload": payload})
    return out


def ready_removed(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Tombstones to deliver on the ONE removed lane: products AND variations Medusa must delete. Served
    while 'pending'; retired on a 'done' ack, dead-lettered after repeated 'blocked' (open reservation).
    E1: PRODUCT tombstones serve before VARIATION tombstones ('product' < 'variation' lexically), so
    Medusa deletes a variety's products before the variety entity (a variant with children can't drop)."""
    rows = ledger.execute(
        "SELECT external_id, kind, source, reason FROM removed WHERE state = 'pending' "
        f"ORDER BY kind, created_at, rowid{_sub_limit(limit)}")
    out = []
    for r in rows:
        if r["kind"] == "variation":
            payload = {"kind": "variation", "key": r["external_id"], "reason": r["reason"]}
        else:
            payload = {"kind": "product", "sku": r["external_id"], "source": r["source"], "reason": r["reason"]}
        out.append({"external_id": r["external_id"], "payload": payload})
    return out


def ready(ledger: Ledger, type_: str, limit: int | None = None) -> list[dict]:
    """Serve the entities of `type_` that are eligible to load now (GET /sync/<type>)."""
    if type_ == "variations":
        return ready_variations(ledger, limit)
    if type_ == "products":
        return ready_products(ledger, limit)
    if type_ == "inventory":
        return ready_inventory(ledger, limit)
    if type_ == "removed":
        return ready_removed(ledger, limit)
    raise ValueError(f"unsupported sync type {type_!r}; expected variations, products, inventory or removed")


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
    if type_ == "removed":
        # tombstone ack: 'done' = Medusa deleted it -> retire the tombstone; 'blocked' = an open
        # reservation blocks the hard-delete -> keep it, count the attempt, re-serve, and dead-letter
        # to 'dead' after _MAX_SYNC_ATTEMPTS so a permanently-reserved id stops re-serving forever.
        if status_ == "done":
            row = ledger.execute("SELECT kind FROM removed WHERE external_id = ?", (external_id,)).fetchone()
            if row and row["kind"] == "variation":
                # E3 + self-heal: hard-delete the variety only once it is FK-safe (no product/combination
                # re-acquired it). A raced produce with a stale retired-key exclusion could re-link a product
                # onto the retiring Key; if so, KEEP the pending tombstone (return without deleting) so it
                # re-serves and self-heals once the next (properly-excluding) produce moves those children
                # off -- never a blind DELETE that would FK-fail and orphan the row with no tombstone.
                child = ledger.execute(
                    "SELECT 1 FROM product WHERE variation_key = ? "
                    "UNION ALL SELECT 1 FROM combination WHERE variation_key = ? LIMIT 1",
                    (external_id, external_id)).fetchone()
                if child:
                    log.warning("retiring variety re-acquired children; keeping its tombstone to re-serve",
                                extra={"extra_fields": {"key": external_id}})
                    return 0
                ledger.execute("DELETE FROM variation WHERE key = ?", (external_id,))
            return ledger.execute("DELETE FROM removed WHERE external_id = ?", (external_id,)).rowcount
        row = ledger.execute("SELECT sync_attempts FROM removed WHERE external_id = ?",
                             (external_id,)).fetchone()
        if not row:
            return 0
        attempts = ((row["sync_attempts"] or 0)) + 1
        state = "dead" if attempts >= _MAX_SYNC_ATTEMPTS else "pending"
        cur = ledger.execute(
            "UPDATE removed SET state = ?, sync_attempts = ?, sync_error = ?, updated_at = ? "
            "WHERE external_id = ?", (state, attempts, (reason or "blocked")[:500], now, external_id))
        if state == "dead" and cur.rowcount:
            log.warning("tombstone dead-lettered after repeated blocks (open reservation?)",
                        extra={"extra_fields": {"external_id": external_id, "attempts": attempts}})
        return cur.rowcount
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
        # same retire guard as the synced branch below: a stray/out-of-order FAILED ack must NOT knock a
        # 'retiring' row back to dirty/gap_held, which would re-enter the serve lane while its tombstone
        # still serves /removed. A failed ack on a retiring row is a no-op (0 rows) by design.
        cur = ledger.execute(
            f"UPDATE {table} SET state = ?, sync_attempts = ?, sync_error = ?, updated_at = ? "
            f"WHERE {pk} = ? AND state != 'retiring'",
            (state, attempts, (reason or "apply failed")[:500], now, external_id))
        if state == "gap_held" and cur.rowcount:
            log.warning("entity dead-lettered after repeated Medusa failures",
                        extra={"extra_fields": {"type": type_, "external_id": external_id,
                                                "attempts": attempts, "reason": reason}})
        return cur.rowcount
    # synced: a PRODUCT is only marked synced if its variation is synced (the eligibility guard).
    guard = " AND variation_key IN (SELECT key FROM variation WHERE state = 'synced')" \
        if type_ == "products" else ""
    # retire guard: a synced ack must NOT resurrect a row that is being RETIRED. A stray/out-of-order ack
    # would otherwise flip a 'retiring' row back to 'synced' while its variation tombstone still serves
    # /removed, diverging the two systems. (ack from pending/syncing/dirty stays allowed by design -- serve
    # leases are an optimization, not a precondition; only 'retiring' is protected.)
    cur = ledger.execute(
        f"UPDATE {table} SET medusa_id = ?, state = 'synced', sync_attempts = 0, sync_error = NULL, "
        f"last_synced = ?, updated_at = ? WHERE {pk} = ? AND state != 'retiring'{guard}",
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
    """The dead-lettered entities and WHY -- the drill-down behind the /status gap_held count, so an
    operator can see what Medusa rejected and fix it. Item: {type, external_id, attempts, error,
    updated_at}. `updated_at` is the last-attempt ISO timestamp (the :4200 failures card renders it)."""
    out: list[dict] = []
    for type_, table in (("variations", "variation"), ("products", "product")):
        pk = _ENTITY[type_][1]
        for r in ledger.execute(
            f"SELECT {pk} AS xid, sync_attempts, sync_error, updated_at FROM {table} "
            f"WHERE state = 'gap_held' ORDER BY updated_at DESC LIMIT ?", (limit,)):
            out.append({"type": type_, "external_id": r["xid"], "attempts": r["sync_attempts"],
                        "error": r["sync_error"], "updated_at": r["updated_at"]})
    for r in ledger.execute(                             # tombstones stuck 'dead' (Medusa kept blocking)
        "SELECT external_id AS xid, sync_attempts, sync_error, updated_at FROM removed "
        "WHERE state = 'dead' ORDER BY updated_at DESC LIMIT ?", (limit,)):
        out.append({"type": "removed", "external_id": r["xid"], "attempts": r["sync_attempts"],
                    "error": r["sync_error"], "updated_at": r["updated_at"]})
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
    """True if a pull is mid-flight: any variation/product is leased ('syncing'). A reset/purge MUST
    refuse while this holds, or it would race an in-flight ack and re-open the ledger drift."""
    for t in ("variation", "product"):
        if ledger.execute(f"SELECT 1 FROM {t} WHERE state = 'syncing' LIMIT 1").fetchone():
            return True
    return False


def _lock_and_check_in_flight(ledger: Ledger) -> None:
    """Take the ledger write lock up front (a no-op write) and re-check in-flight WITHIN this
    transaction, so a concurrent pull (separate process) cannot lease/ack between the check and the
    mutation; with busy_timeout it waits for our commit. Raises ServeInFlight -> caller maps to 409.
    Shared by reset_sync_state and purge_discontinued (both are destructive, both must not race a pull)."""
    ledger.execute("UPDATE ledger_meta SET updated_at = ? WHERE id = 1", (now_iso(),))
    # A crashed puller (e.g. the ECS task recycled by a deploy mid-serve) leaves DEAD 'syncing' leases.
    # reap_stale_syncing only ran at the head of a SERVE, so those dead leases would 409 a reset/purge
    # forever on a pull that no longer exists -- the operator gets wedged (reset refused, every re-pull
    # clipped by the next deploy). Reclaim stale leases here FIRST, so only a LIVE pull (lease < TTL) still
    # blocks; a killed pull can never permanently wedge a reset. Same TTL/semantics as the serve path.
    reap_stale_syncing(ledger)
    if serve_in_flight(ledger):
        raise ServeInFlight("a pull is in flight; refusing to mutate mid-serve")


def _reset_overlay(ledger: Ledger, table: str, now: str, where: str = "", params: tuple = ()) -> int:
    """Reset ONE table's sync overlay: state -> 'pending', drop medusa_id/last_synced/sync bookkeeping.
    Content columns (name, type, image, price...) are untouched. `where`/`params` scope the rows."""
    cols = {r["name"] for r in ledger.execute(f"PRAGMA table_info({table})")}
    # E4/E6: a 'retiring' row is mid-removal (its variation-tombstone is pending). A reset must NOT flip it
    # back to 'pending' (re-serving a removed variety) AND must keep its medusa_id + bookkeeping -- the
    # medusa_id is load-bearing for the retire/ack/un_retire state machine (it says "Medusa has this, delete
    # it"). Everything NOT retiring re-serves from zero.
    sets = ["state = CASE WHEN state = 'retiring' THEN 'retiring' ELSE 'pending' END", "updated_at = ?"]
    sets += [f"{c} = CASE WHEN state = 'retiring' THEN {c} ELSE NULL END"
             for c in ("medusa_id", "last_synced", "sync_error") if c in cols]
    if "sync_attempts" in cols:
        sets.append("sync_attempts = CASE WHEN state = 'retiring' THEN sync_attempts ELSE 0 END")
    return ledger.execute(f"UPDATE {table} SET {', '.join(sets)}{where}", (now, *params)).rowcount


def reset_sync_state(ledger: Ledger, source_codes: list[str] | None = None,
                     hard: bool = False, prune_stale: bool = False) -> dict[str, int]:
    """CLEAN START of the sync overlay, so the ledger stops drifting from a wiped Medusa.

    soft (default): every entity -> 'pending' and all Medusa ids + sync bookkeeping dropped; inventory
      baseline cleared. Re-serves the whole catalog from zero WITHOUT re-scraping.
    hard: additionally DELETE the scraped products + inventory (the scraper's per-source output), so a
      re-scrape rebuilds them from scratch.
    prune_stale (global only): tombstone every variation that dropped OUT of the current catalogue
      (in_full = 0, no products) so Medusa CONVERGES to the seed -- deleting re-key old sides and dropped
      varieties instead of leaving them as stale entities. This is what makes the factory reset a true
      cold start on BOTH sides (a plain reset only zeroes sync state; it never removed a stale entity).

    INVARIANT: variation/backbone rows in the CURRENT catalogue are NEVER deleted -- they are your base
    config (also held in git as variants_export_base + backbone_*), and re-seeding them from the live
    export would re-introduce stale synced ids (the drift we are fixing). Only their sync OVERLAY is
    cleared. prune_stale removes ONLY in_full=0 rows (already out of the catalogue), never a live variety.

    `source_codes` scopes products (+ their stock) to those sources; the shared base layer
    (variations/attributes/combinations) is reset only on a GLOBAL (unscoped) reset. Returns row counts.
    """
    now, out = now_iso(), {}
    _lock_and_check_in_flight(ledger)          # atomic guard: refuse if a pull holds a lease
    scoped = source_codes is not None          # empty list -> scope-to-nothing, NOT a global reset
    where, params = ("", ())
    if scoped:
        marks = ",".join("?" * len(source_codes)) or "NULL"   # source IN (NULL) matches no row
        where, params = (f" WHERE source IN ({marks})", tuple(source_codes))

    # Prune BEFORE the overlay reset: prune sets stale rows 'retiring' + tombstones them, and _reset_overlay
    # preserves 'retiring' (never flips a mid-removal row). Global-only, like the shared-base overlay reset.
    if prune_stale and not scoped:
        out["pruned_stale"] = prune_stale_variations(ledger)["pruned"]

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


def record_tombstones(ledger: Ledger, pairs: list[tuple], reason: str = "discontinued",
                      kind: str = "product") -> int:
    """Record a tombstone per removed entity on the ONE `removed` lane, so Medusa pulls the deletion on
    GET /sync/removed, deletes its own entity, and acks. `kind` says what `external_id` addresses: a
    product SKU (kind='product', default) or a variation Key (kind='variation'). `pairs` is
    [(external_id, source), ...] (source is null for variation tombstones). Resets an existing tombstone
    to 'pending' so a re-purge/re-retire re-serves."""
    now = now_iso()
    for external_id, source in pairs:
        ledger.upsert("removed", {"external_id": external_id, "kind": kind, "source": source,
                                  "reason": reason, "state": "pending", "sync_attempts": 0,
                                  "sync_error": None, "created_at": now, "updated_at": now},
                      pk=("external_id",))
    return len(pairs)


def purge_discontinued(ledger: Ledger, source_codes: list[str] | None = None,
                       reason: str = "discontinued") -> dict:
    """Hard-delete the qty-0 (delisted / dead-stock) products and their inventory rows, so the
    graveyard of never-returning products stops accumulating. A product that reappears in a later
    scrape recreates cleanly (no prior ledger row -> populate treats it as NEW, and clears any leftover
    tombstone). Variation/backbone rows are untouched. `source_codes` scopes to those sources. Records a
    tombstone per deleted SKU (`reason` tags why: discontinued vs vendor_removed) so Medusa deletes the
    same set (product + scraper_sync_ref) via the pull lane. Returns {product, inventory, external_ids}."""
    _lock_and_check_in_flight(ledger)          # same atomic guard as reset -- never purge mid-serve
    scope = ""
    params: tuple = ()
    if source_codes is not None:
        marks = ",".join("?" * len(source_codes)) or "NULL"
        scope, params = (f" AND p.source IN ({marks})", tuple(source_codes))
    # dead stock = a product whose inventory qty is 0 (populate_discontinued / a drop set it there)
    rows = ledger.execute(
        f"SELECT p.sku AS sku, p.source AS source FROM product p JOIN inventory i ON i.sku = p.sku "
        f"WHERE i.qty = 0{scope}", params).fetchall()
    skus = [r["sku"] for r in rows]
    if not skus:
        return {"product": 0, "inventory": 0, "external_ids": []}
    record_tombstones(ledger, [(r["sku"], r["source"]) for r in rows], reason)   # deliver the delete to Medusa
    q = ",".join("?" * len(skus))
    inv = ledger.execute(f"DELETE FROM inventory WHERE sku IN ({q})", tuple(skus)).rowcount   # child first (FK)
    prod = ledger.execute(f"DELETE FROM product WHERE sku IN ({q})", tuple(skus)).rowcount
    log.warning("ledger PURGE discontinued (qty-0) products", extra={"extra_fields": {
        "sources": source_codes or "all", "reason": reason, "product": prod, "inventory": inv}})
    return {"product": prod, "inventory": inv, "external_ids": skus}


def delist_source(ledger: Ledger, source_codes: list[str] | None = None) -> dict:
    """Stage 1 of a vendor removal ('Take offline'): set every one of a source's products to qty 0, so
    the next pull serves a qty-0 inventory delta and Medusa takes them out of sale. Reversible -- a
    later re-scrape restocks them at qty > 0 -- so the caller ALSO disables the source, or a scrape
    would silently re-stock a vendor you took offline. Products and their sync ids stay in place; only
    the stock changes. `source_codes` scopes to those sources. Returns {delisted, external_ids}."""
    _lock_and_check_in_flight(ledger)          # same atomic guard as reset/purge -- never mutate mid-serve
    scope, params = "", ()
    if source_codes is not None:
        marks = ",".join("?" * len(source_codes)) or "NULL"
        scope, params = (f" AND p.source IN ({marks})", tuple(source_codes))
    skus = [r["sku"] for r in ledger.execute(
        f"SELECT p.sku FROM product p JOIN inventory i ON i.sku = p.sku WHERE i.qty != 0{scope}", params)]
    if not skus:
        return {"delisted": 0, "external_ids": []}
    q = ",".join("?" * len(skus))
    # reason='delisted' rides the inventory delta so Medusa can HIDE (status->draft) exactly these SKUs,
    # provenance-safe, and tell them apart from an ordinary out-of-stock (qty 0, reason null -> stays listed).
    ledger.execute(f"UPDATE inventory SET qty = 0, reason = 'delisted', updated_at = ? WHERE sku IN ({q})",
                   (now_iso(), *skus))
    log.warning("ledger DELIST source (qty -> 0)", extra={"extra_fields": {
        "sources": source_codes or "all", "delisted": len(skus)}})
    return {"delisted": len(skus), "external_ids": skus}


def live_products(ledger: Ledger, source_codes: list[str] | None = None) -> int:
    """How many of a source's products are still LIVE (inventory qty > 0). The vendor-removal DELETE
    guard uses this: a source with live products must be taken offline (delisted to qty 0, pulled by
    Medusa) before it can be removed, so nothing is left orphaned-live in the shop."""
    scope, params = "", ()
    if source_codes is not None:
        marks = ",".join("?" * len(source_codes)) or "NULL"
        scope, params = (f" AND p.source IN ({marks})", tuple(source_codes))
    row = ledger.execute(
        f"SELECT COUNT(*) AS n FROM product p JOIN inventory i ON i.sku = p.sku WHERE i.qty > 0{scope}",
        params).fetchone()
    return int(row["n"]) if row else 0


def variation_live_products(ledger: Ledger, key: str) -> int:
    """How many LIVE (qty > 0) products a variety still has. retire_variation guards on this (E11): a
    variety with live products must have them delisted/moved first, or `force` cascades them."""
    row = ledger.execute("SELECT COUNT(*) AS n FROM product p JOIN inventory i ON i.sku = p.sku "
                         "WHERE p.variation_key = ? AND i.qty > 0", (key,)).fetchone()
    return int(row["n"]) if row else 0


def retire_variation(ledger: Ledger, key: str, force: bool = False,
                     reason: str = "variation_removed") -> dict:
    """Cascade a variety's explicit removal (also the old side of a re-key). Its products (which belong to
    it) are tombstoned + hard-deleted; its combinations (the dormant lane) are deleted so the variety row
    is FK-safe to drop; then EITHER a variation tombstone is recorded and the row held 'retiring' until
    Medusa acks the delete (only if it was ever synced -- E2), OR the row is dropped locally now (a
    never-synced variety: Medusa never received it, so there is nothing to tell it to delete). The caller
    (lifecycle) has already guarded existence + the live-product precondition. Serve-in-flight guarded."""
    _lock_and_check_in_flight(ledger)
    var = ledger.get("variation", "key", key)
    prods = ledger.execute("SELECT sku, source FROM product WHERE variation_key = ?", (key,)).fetchall()
    if prods:                                  # E1: product tombstones (serve before the variation one)
        record_tombstones(ledger, [(p["sku"], p["source"]) for p in prods],
                          reason="vendor_removed", kind="product")
        skus = [p["sku"] for p in prods]
        q = ",".join("?" * len(skus))
        ledger.execute(f"DELETE FROM inventory WHERE sku IN ({q})", tuple(skus))   # child first (FK)
        ledger.execute(f"DELETE FROM product WHERE sku IN ({q})", tuple(skus))
    ledger.execute("DELETE FROM combination WHERE variation_key = ?", (key,))      # dormant lane, FK child
    synced = bool(var and var["medusa_id"])
    if synced:                                 # E2: only tell Medusa to delete what it actually received
        record_tombstones(ledger, [(key, None)], reason=reason, kind="variation")
        ledger.set_state("variation", "key", key, "retiring")   # hold until the delete is acked (E3)
    else:
        ledger.execute("DELETE FROM variation WHERE key = ?", (key,))              # never synced -> drop now
    log.warning("variety RETIRED", extra={"extra_fields": {
        "key": key, "products_removed": len(prods), "tombstoned_variation": synced, "force": force}})
    return {"retired": key, "products_removed": len(prods), "tombstoned_variation": synced}


def prune_stale_variations(ledger: Ledger) -> dict:
    """Tombstone every variation that has DROPPED OUT of the current catalogue (in_full = 0) and has no
    products, so Medusa converges to the seed instead of keeping stale entities (re-key old sides, dropped
    varieties). Returns {pruned, keys}.

    Tombstones by KEY UNCONDITIONALLY -- not gated on medusa_id like retire_variation. That gate is wrong
    here: a prior reset drops medusa_id, so a stale row that Medusa DOES hold would look 'never synced' and
    be deleted locally with NO tombstone, leaving Medusa an orphan forever (the 'seed vs Medusa out of line'
    fault). Blokport deletes by key (deleteScraperVariationByKey) and no-ops a key it never had, so an
    unconditional key-tombstone is safe both ways. The row is held 'retiring' until Medusa acks the delete
    (an in_full=0 row is already excluded from the served catalogue, so holding it costs nothing).

    Only touches products-free rows: a variation that still has products is live -- never dropped here. The
    caller runs this INSIDE the reset's write lock, BEFORE the overlay reset (which preserves 'retiring')."""
    keys = [r["key"] for r in ledger.execute(
        "SELECT key FROM variation v WHERE v.in_full = 0 AND v.state != 'retiring' "
        "AND NOT EXISTS (SELECT 1 FROM product p WHERE p.variation_key = v.key)")]
    for key in keys:
        ledger.execute("DELETE FROM combination WHERE variation_key = ?", (key,))   # dormant FK child
        record_tombstones(ledger, [(key, None)], reason="stale_not_in_seed", kind="variation")
        ledger.set_state("variation", "key", key, "retiring")
    if keys:
        log.warning("pruned stale variations (not in current seed catalogue)", extra={"extra_fields": {
            "pruned": len(keys)}})
    return {"pruned": len(keys), "keys": keys}


def un_retire_variation(ledger: Ledger, key: str) -> dict:
    """Reverse a retire: flip a 'retiring' variety back to serving and clear its pending variation
    tombstone, so a re-mint / the operator's undo is not deleted by a stale tombstone (mirrors source
    resume). A never-synced retire deleted the row already; the exclusion-memory clear (caller) lets the
    next produce re-mint it fresh."""
    _lock_and_check_in_flight(ledger)
    var = ledger.get("variation", "key", key)
    if var is not None and var["state"] == "retiring":
        ledger.set_state("variation", "key", key, "dirty" if var["medusa_id"] else "pending")
    cleared = ledger.execute("DELETE FROM removed WHERE kind = 'variation' AND external_id = ?",
                             (key,)).rowcount
    return {"un_retired": key, "tombstone_cleared": cleared}


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
