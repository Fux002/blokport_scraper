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
product is served only once its variation is synced AND that variety has an icon in the
product's category (the H2 hold, via variation_category_image), and stock only once Medusa holds its
product (it acked the product's id). One gate is still deferred: every referenced
attribute synced (all attributes are synced after bootstrap; NEW values are the
dormant C3 case). No em dashes (design principle 2).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.ledger.db import (CLEAR_STOCK_FAILURES_SQL, FORGET_SYNCED_STOCK_SQL, KEPT_SKUS_SQL,
                                      RECORD_SYNCED_STOCK_SQL,
                                      RESERVE_SQL, STATES, STOCK_DELTA_SQL, UNHIDE_SQL, Ledger, in_medusa_sql,
                                      now_iso, on_sale_sql, variation_icons)
from stone_pipeline.ledger.payloads import product_payload, variation_payload

log = logfmt.get_logger("ledger.sync")


class ServeInFlight(Exception):
    """A reset was attempted while a pull holds a lease ('syncing' rows). The caller maps it to 409."""


def _retry_after(attempts: int) -> str:
    """When an entity that just failed for the attempts-th time in a row may be served again (SETTINGS.sync)."""
    wait = SETTINGS.sync.retry_base_seconds * SETTINGS.sync.retry_factor ** (attempts - 1)
    return (datetime.now(timezone.utc) + timedelta(seconds=wait)).isoformat()


# A served-but-not-yet-acked entity is leased in state 'syncing' (the in-flight guard) so two overlapping pulls can
# NEVER double-serve the same rows. If Medusa pulls but never acks (crash / lost response), the lease is reclaimed to
# 'dirty' after SETTINGS.sync.syncing_lease_seconds and re-served.
def reap_stale_syncing(ledger: Ledger, table: str | None = None,
                       older_than_s: int | None = None) -> int:
    """Return dead leases ('syncing' older than `older_than_s`) to 'dirty' so they re-serve.
    Called lazily at the head of every serve, so a crashed puller's in-flight rows recover on the
    next pull without a background job. POST /sync/v1/release-leases calls it with 0: the consumer's
    run, the ledger's one puller under its catalog lock, declares every lease dead before its first
    pull (PHASE-0 Q8). `older_than_s` None = the lease (SETTINGS.sync.syncing_lease_seconds). Returns the number
    reclaimed."""
    if older_than_s is None:
        older_than_s = SETTINGS.sync.syncing_lease_seconds
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_s)).isoformat()
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
}


# --- GET /sync/status ---------------------------------------------------------

# The products a change waits on (pending or dirty, not a dead-letter) that are held back because they link a
# standardised file the photos side has not published: /status counts them, /failures lists them. `{cols}` is the
# select list. A synced product with the fact is not waiting on anything, so it is neither.
_PHOTO_HELD = ("SELECT {cols} FROM product WHERE photo_unpublished IS NOT NULL AND state IN ('pending', 'dirty') "
               "AND abandoned_at IS NULL")


# The varieties a change waits on (pending or dirty, rendered, not a dead-letter) held back because their alias_rows or
# codes are NULL, not computed yet (ledger v18): a produce computes both, then they serve. /status counts them.
_ALIASES_UNCOMPUTED = ("SELECT COUNT(*) AS n FROM variation WHERE state IN ('pending', 'dirty') AND in_full = 1 "
                       "AND abandoned_at IS NULL AND (alias_rows IS NULL OR codes IS NULL)")


def status(ledger: Ledger) -> dict[str, dict[str, int]]:
    """Per-type state counts. Each state-bearing table reports every state it can hold (db.STATES), 0 for one
    with no rows: a count is always a value, never an absent key;
    inventory reports total rows, the `delta` count (exactly what the inventory lane would serve now), the `dead`
    count (dead-lettered stock rows not abandoned, like gap_held) and the `waiting` count (un-hides waiting on their
    product's own sync)."""
    out: dict[str, dict[str, int]] = {}
    for table, states in STATES.items():
        held = ledger.counts(table)
        out[table] = {state: held.get(state, 0) for state in states}
    total = ledger.execute("SELECT COUNT(*) AS n FROM inventory").fetchone()["n"]
    dead = ledger.execute(f"SELECT COUNT(*) AS n FROM inventory i WHERE {_STOCK_DEAD}").fetchone()["n"]
    waiting = ledger.execute(f"SELECT COUNT(*) AS n {_UNHIDE_WAITING}", (now_iso(),)).fetchone()["n"]
    out["inventory"] = {"total": total, "delta": _count_inventory_lane(ledger), "dead": dead, "waiting": waiting}
    # E13: `orphaned` = synced, rendered (in_full=1) varieties with 0 products. DEPRECATED, to be removed
    # (C13): under the union seed model a rendered variety with no scraped product is a valid base/seed
    # catalogue member (dev 11-06: 36,077 of 36,600), so this is NOT an actionable drift signal and cannot
    # be "driven to zero". The real removals surface elsewhere: an explicit removal or a re-key old side is
    # held `retiring` (retire_variation), already a `variation` state count; a variety dropped from the
    # catalogue is in_full=0 (excluded here) and pruned to `retiring` at reset. Value kept unchanged until
    # Blokport stops reading it (Release 2).
    orphaned = ledger.execute(
        "SELECT COUNT(*) AS n FROM variation v WHERE v.state = 'synced' AND v.in_full = 1 "
        "AND NOT EXISTS (SELECT 1 FROM product p WHERE p.variation_key = v.key)").fetchone()
    out["variation_reconcile"] = {"orphaned": orphaned["n"] or 0}
    # products a change waits on that are held back because they link a photo the photos side has not published
    held = ledger.execute(_PHOTO_HELD.replace("{cols}", "COUNT(*) AS n")).fetchone()["n"]
    out["product_photos"] = {"unpublished": held}
    # varieties a change waits on that are held back because their alias_rows or codes are not computed yet (v18)
    out["variation_aliases"] = {"uncomputed": ledger.execute(_ALIASES_UNCOMPUTED).fetchone()["n"]}
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
    # VE2: an archived variety is frozen (never re-served), so it can never clear. After a reset nulls its
    # medusa_id it would otherwise sit in this held count forever and mask the reset's converged (held == 0)
    # check, so exclude it: a frozen variety is not an awaiting-pull variety.
    new_pending = "in_full = 1 AND medusa_id IS NULL AND state IN ('pending', 'dirty') AND archived = 0"
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


def frozen_waiting_products(ledger: Ledger) -> int:
    """New products (NOT Medusa-held) bound to an ARCHIVED variety. The VE2 freeze means the products lane
    never serves them, so they wait silently as pending. The produce logs this count so the hold is visible;
    the VISIBLE upstream hold (curate refusing a new bind to an archived variety) is PR5's, not PR3's."""
    return ledger.execute(
        "SELECT COUNT(*) AS n FROM product p JOIN variation v ON v.key = p.variation_key "
        f"WHERE v.archived = 1 AND p.state IN ('pending', 'dirty') AND NOT {in_medusa_sql('p')}").fetchone()["n"]


# --- GET /sync/<type>?status=ready --------------------------------------------

def _sub_limit(limit: int | None) -> str:
    return f" LIMIT {int(limit)}" if (limit and int(limit) > 0) else ""


# ONE source of truth for "eligible to serve", reused by the lease (ready_*) AND the non-leasing
# peek (count_ready) so the two can never diverge -- a variation is servable once produced + typed;
# a product once its variation is LIVE (synced, typed) and has an icon in the product's own category (D11: both
# columns hold the category HANDLE, so the gate joins equal values). `v` is the joined variation alias. Neither while
# it waits out a failed apply (retry_after): each takes the current time as its one parameter.
# VE2: an archived variation is FROZEN -- the variations lane never serves it (no create, no update), and the
# products lane admits it only for an EXISTING product (one Medusa holds, in_medusa_sql): a new product on an
# archived variety is never served. archived beats dirty. Both are a pure mirror of the export (writethrough).
# Nor a product linking a standardised file the photos side has not published (photo_unpublished, recomputed by every
# produce: populate.mark_unpublished_photos): a content fact, so a requeue or a reset never revives it.
_ELIGIBLE_VARIATION = ("state IN ('pending', 'dirty') AND in_full = 1 AND archived = 0 "
                       "AND type IS NOT NULL AND type != '' AND alias_rows IS NOT NULL AND codes IS NOT NULL "
                       "AND (retry_after IS NULL OR retry_after <= ?)")
_ELIGIBLE_PRODUCT = ("p.state IN ('pending', 'dirty') AND p.photo_unpublished IS NULL "
                     f"AND ((v.state = 'synced' AND v.archived = 0) OR (v.archived = 1 AND {in_medusa_sql('p')})) "
                     "AND v.type IS NOT NULL AND v.type != '' "
                     "AND EXISTS (SELECT 1 FROM variation_category_image ci "
                     "WHERE ci.key = v.key AND ci.category_handle = p.category) "
                     "AND (p.retry_after IS NULL OR p.retry_after <= ?)")


def count_ready(ledger: Ledger, type_: str) -> int:
    """Non-leasing PEEK: how many rows COULD serve right now, WITHOUT leasing them. ready() would
    mark the rows 'syncing' and consume them, so use this for convergence checks, monitoring, and
    any assertion that must not have a side effect. Same eligibility predicates as ready()."""
    if type_ in ("variations", "variation"):
        return ledger.execute("SELECT COUNT(*) n FROM variation WHERE " + _ELIGIBLE_VARIATION, (now_iso(),)).fetchone()["n"]
    if type_ in ("products", "product"):
        return ledger.execute(
            "SELECT COUNT(*) n FROM product p JOIN variation v ON v.key = p.variation_key "
            "WHERE " + _ELIGIBLE_PRODUCT, (now_iso(),)).fetchone()["n"]
    if type_ == "inventory":
        return _count_inventory_lane(ledger)
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
    now = now_iso()
    rows = ledger.execute(
        "UPDATE variation SET state = 'syncing', reserved_at = NULL, updated_at = ? WHERE key IN ("
        "  SELECT key FROM variation WHERE " + _ELIGIBLE_VARIATION +
        "  ORDER BY created_at, key" + _sub_limit(limit) + ") "
        "RETURNING key, type, name, alias_rows, codes, payload_hash",
        (now, now)).fetchall()
    icons = variation_icons(ledger, [v["key"] for v in rows])
    return _isolate(ledger, "variation", "key", rows, lambda v: {
        "external_id": v["key"], "payload_hash": v["payload_hash"],
        "payload": variation_payload(v, icons.get(v["key"], []))})


def ready_products(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Products awaiting sync whose VARIATION is synced AND has an icon in the product's category.

    The icon gate is the H2 hold decision: a product never lists until its variety's icon
    in that category exists, so it is not served with scraped photos only. A
    variation_category_image row exists exactly when emit listed that pair on S3, so the
    gate needs no extra S3 call.

    The variation must also be TYPED: Blokport checks the product's type against its
    variety's, so a synced-but-untyped variation would bind the product to an empty type.
    Gating on `v.type` holds those products until the variety is typed.

    The payload (payloads.product_payload) carries ZERO Medusa ids (the review's red flag): everything is an
    external reference Medusa resolves on its side. `vendor` (the source) resolves to the
    company + sales channel. `port_ids` are the SUPPLIER's shipping ports (already Medusa
    port ids, from ports.csv) -- the true port of origin (where the goods ship FROM), sent
    resolved so Medusa links them directly and does NOT derive ports from the quarry
    `origin_country_code` (that gave every product all ports in its quarry country).
    `image_urls` are ingestion sources Medusa copies into its own storage."""
    # atomically LEASE the eligible products to 'syncing' (in-flight guard, same as variations).
    reap_stale_syncing(ledger, "product")
    now = now_iso()
    rows = ledger.execute(
        "UPDATE product SET state = 'syncing', updated_at = ? WHERE sku IN ("
        "  SELECT p.sku FROM product p JOIN variation v ON v.key = p.variation_key "
        "  WHERE " + _ELIGIBLE_PRODUCT +
        "  ORDER BY p.created_at, p.sku" + _sub_limit(limit) + ") RETURNING *",
        (now, now)).fetchall()
    return _isolate(ledger, "product", "sku", rows, lambda p: {
        "external_id": p["sku"], "payload_hash": p["payload_hash"], "payload": product_payload(p)})


# The serve gate of an inventory row (aliases i, p): every count move and every hide goes out for a product Medusa
# holds (db.in_medusa_sql), but an un-hide (db.UNHIDE_SQL) waits until the product itself is synced, so a hidden product
# never comes back on sale before its own update (a new photo, photo_ready) has reached Medusa. The serve only: an ack
# of what was served is recorded whatever the product did since (_ack_inventory).
_STOCK_GATE = f"{in_medusa_sql('p')} AND (p.state = 'synced' OR NOT {UNHIDE_SQL})"

# An un-hide held back only by the serve gate (GET /status `waiting`): a stock delta of a product Medusa holds that
# would bring it back on sale while the product itself is not synced yet, and that nothing else holds (not dead, not
# abandoned, not waiting out a failed ack). Parameter: the current time.
_UNHIDE_WAITING = ("FROM inventory i JOIN product p ON p.sku = i.sku "
                   f"WHERE {in_medusa_sql('p')} AND p.state != 'synced' AND {UNHIDE_SQL} AND {STOCK_DELTA_SQL} "
                   f"AND i.sync_attempts < {SETTINGS.sync.max_attempts} AND i.abandoned_at IS NULL "
                   "AND (i.retry_after IS NULL OR i.retry_after <= ?)")

# A dead stock row (alias i): the failed/refused acks reached the cap, and the operator has not abandoned it.
_STOCK_DEAD = f"i.sync_attempts >= {SETTINGS.sync.max_attempts} AND i.abandoned_at IS NULL"

# ONE definition of the inventory lane, used by the serve, the non-leasing peek and GET /status's `delta`, so the
# three never disagree: a stock delta through the stock gate, not waiting out a failed ack (retry_after), not dead or
# abandoned (sync_attempts at the product lane's cap). A bundle replaced by its lot waits until a piece of that lot
# exists in Medusa and is on sale (db.on_sale_sql: Blokport hides a sold or delisted piece), so the redirect Blokport
# derives from it never points at an empty lot page; a piece counts when on sale or with no stock row yet (the LEFT
# JOIN). Parameters: the current time, then the dead-letter cap.
_INVENTORY_LANE = (
    "FROM inventory i JOIN product p ON p.sku = i.sku "
    f"WHERE {_STOCK_GATE} AND {STOCK_DELTA_SQL} "
    "AND (i.retry_after IS NULL OR i.retry_after <= ?) AND i.sync_attempts < ? AND i.abandoned_at IS NULL "
    "AND (i.reason IS NOT 'replaced_by_lot' OR EXISTS (SELECT 1 FROM product q LEFT JOIN inventory j ON j.sku = q.sku "
    f"     WHERE q.lot_code = i.replaced_by_lot_code AND {in_medusa_sql('q')} "
    f"    AND (j.sku IS NULL OR {on_sale_sql('j')}))) ")


def _count_inventory_lane(ledger: Ledger) -> int:
    return ledger.execute("SELECT COUNT(*) AS n " + _INVENTORY_LANE,
                          (now_iso(), SETTINGS.sync.max_attempts)).fetchone()["n"]


def ready_inventory(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Stock deltas to push (the inventory lane, _INVENTORY_LANE). The lane does not lease: a row stays a delta until
    its ack records the stock as synced, and a failed or refused ack makes it wait like a failed product."""
    rows = ledger.execute(
        "SELECT i.sku AS sku, i.qty AS qty, i.made_to_order AS made_to_order, i.reason AS reason, "
        "i.replaced_by_lot_code AS lot_code, i.gone_at AS gone_at " + _INVENTORY_LANE +
        "ORDER BY i.sku" + _sub_limit(limit), (now_iso(), SETTINGS.sync.max_attempts))
    out = []
    for r in rows:
        # quantity is null exactly when made_to_order (on sale with no count)
        payload = {"sku": r["sku"], "quantity": r["qty"], "made_to_order": bool(r["made_to_order"])}
        # 'sold_out' -> stays published, not buyable; 'delisted' / 'no_photo' -> Medusa hides; 'replaced_by_lot' ->
        # Medusa deactivates + 301s to the lot (lot_code); absent -> a plain stock (table 505, agreed with M4)
        if r["reason"]:
            payload["reason"] = r["reason"]
        if r["reason"] == "replaced_by_lot":
            payload["lot_code"] = r["lot_code"]
        if r["gone_at"]:                       # gone at its supplier since then (a sold-out or hidden listing)
            payload["gone_at"] = r["gone_at"]
        out.append({"external_id": r["sku"], "payload": payload})
    return out


def ready_removed(ledger: Ledger, limit: int | None = None) -> list[dict]:
    """Tombstones to deliver on the ONE removed lane: products AND variations Medusa must delete. Served
    while 'pending'; retired on a 'done' ack, dead-lettered after repeated 'blocked' (open reservation).
    E1: PRODUCT tombstones serve before VARIATION tombstones ('product' < 'variation' lexically), so
    Medusa deletes a variety's products before the variety entity (a variant with children can't drop)."""
    rows = ledger.execute(
        "SELECT external_id, kind, source, reason FROM removed WHERE state = 'pending' "
        "AND (retry_after IS NULL OR retry_after <= ?) "
        f"ORDER BY kind, created_at, rowid{_sub_limit(limit)}", (now_iso(),))
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

def _ack_inventory(ledger: Ledger, sku: str, failed: bool, reason: str | None,
                   quantity: int | None, made_to_order: bool | None) -> int:
    """One inventory ack, for a product Medusa holds (db.in_medusa_sql). A success whose echo matches the current stock
    is always recorded as synced, whatever the product did since the serve (the un-hide gate is the serve's only: a
    dropped ack would leave Medusa on sale with no delta left to correct it), and clears the failure bookkeeping.
    A failure, or a success the echo guard refuses, counts an attempt and waits like a failed product (_retry_after,
    dead at SETTINGS.sync.max_attempts): without it the row stays a delta at the head of the sku-ordered page and
    re-serves on every pull. Only a row that is a delta now counts a failure: a late or duplicate 'failed' for a stock
    already synced is no failure of anything still to send. Returns 1 when the stock was recorded as synced or the
    failure recorded, 0 for an unknown sku, a product not in Medusa, a refused echo, or a failure on a row with
    nothing to send.

    F4 lost-update guard: the stock is recorded only if the SERVED stock is still the current one. Each stock field
    the ack echoes must equal the live one: a produce that moved stock between serve and ack makes it differ, so the
    ack is refused and the NEW stock re-serves once the wait has passed (one backoff, 5 min) -- instead of recording
    as synced a stock Medusa never received. A made-to-order echo has quantity null: its made_to_order = 1 alone pins
    the stock (qty is NULL exactly then). An unechoed field is not checked."""
    cond, params = "1", []
    if quantity is not None:
        cond, params = cond + " AND i.qty = ?", params + [quantity]
    if made_to_order is not None:
        cond, params = cond + " AND i.made_to_order = ?", params + [1 if made_to_order else 0]
    row = ledger.execute(f"SELECT i.qty, i.made_to_order, i.sync_attempts, ({cond}) AS echo_current, "
                         f"({STOCK_DELTA_SQL}) AS is_delta "
                         f"FROM inventory i JOIN product p ON p.sku = i.sku WHERE i.sku = ? AND {in_medusa_sql('p')}",
                         (*params, sku)).fetchone()
    if row is None:
        return 0
    if failed:
        if not row["is_delta"]:
            return 0
        return _fail_inventory(ledger, sku, row["sync_attempts"], reason or "apply failed")
    if not row["echo_current"]:
        if row["is_delta"]:
            _fail_inventory(ledger, sku, row["sync_attempts"],
                            f"refused: the ack echoed quantity {quantity!r}, made_to_order {made_to_order!r}; the "
                            f"stock is quantity {row['qty']!r}, made_to_order {bool(row['made_to_order'])!r}")
        return 0
    return ledger.execute(f"UPDATE inventory SET {RECORD_SYNCED_STOCK_SQL}, {CLEAR_STOCK_FAILURES_SQL}, "
                          "updated_at = ? WHERE sku = ?", (now_iso(), sku)).rowcount


def _fail_inventory(ledger: Ledger, sku: str, attempts_before: int, error: str) -> int:
    """Count one failed attempt on an inventory row: it waits _retry_after, and at SETTINGS.sync.max_attempts it is
    dead (the lane stops serving it; GET /failures lists it until a requeue, or a new stock, clears it)."""
    attempts = attempts_before + 1
    cur = ledger.execute("UPDATE inventory SET sync_attempts = ?, sync_error = ?, retry_after = ?, updated_at = ? "
                         "WHERE sku = ?", (attempts, error[:500], _retry_after(attempts), now_iso(), sku))
    if attempts >= SETTINGS.sync.max_attempts:
        log.warning("stock dead-lettered after repeated failed acks",
                    extra={"extra_fields": {"sku": sku, "attempts": attempts, "error": error}})
    return cur.rowcount


# The closed set of ack statuses per lane (MEDUSA_INTEGRATION_GUIDE.md section 7), as (success, failure). Blokport sends
# created/updated/skipped/failed on variations and products, updated/skipped/failed on inventory and done/blocked/failed
# on removed. An inventory 'skipped' is a stock Blokport did NOT apply (SKU not found, or its stock write refused it),
# so it is a failure there. 'synced' is ack_batch's internal default for an ack that names no status (Blokport always
# names one).
_ACK_STATUSES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "variations": (frozenset({"created", "updated", "skipped", "synced"}), frozenset({"failed"})),
    "products": (frozenset({"created", "updated", "skipped", "synced"}), frozenset({"failed"})),
    "inventory": (frozenset({"updated", "synced"}), frozenset({"skipped", "failed"})),
    "removed": (frozenset({"done"}), frozenset({"blocked", "failed"})),
}


def ack(ledger: Ledger, type_: str, external_id: str, medusa_id: str | None = None,
        status_: str = "synced", reason: str | None = None,
        payload_hash: str | None = None, quantity: int | None = None,
        made_to_order: bool | None = None) -> int:
    """Medusa talks back for ONE entity. Returns the number of ledger rows changed (0 = the
    external_id was not found, or the ack was REFUSED as out-of-order -- see the eligibility guard).
    An ack the ledger cannot apply fails loud: a status outside the lane's set (_ACK_STATUSES), or on
    variations/products a success without a medusa_id, is recorded as a FAILED attempt with that error
    (it backs off, dead-letters at the cap and is listed in GET /failures). It never writes the id, so a
    stored Medusa id is never cleared (a row without one is dropped locally on retire, with no tombstone).
    Raises ValueError for an unknown lane (nothing to record it on).
      success -> record the minted/matched id, mark synced, clear the failure counter+reason.
      failure -> record the reason and re-serve (dirty) once its retry_after has passed (_retry_after, growing
                 per consecutive failure); after SETTINGS.sync.max_attempts consecutive failures the entity
                 is DEAD-LETTERED to 'gap_held' (stops serving, surfaces in /status).
    Inventory: see _ack_inventory; removed: see _ack_removed.

    ELIGIBILITY GUARD (defense in depth): a product is only marked synced if its variation is
    already synced, and stock only if Medusa holds its product -- so a duplicate/out-of-order ack can
    never load a product ahead of its variation (which the serve gate already prevents, but the ack
    path must not trust the caller)."""
    if type_ not in _ACK_STATUSES:
        raise ValueError(f"unsupported sync type {type_!r}")
    success, failure = _ACK_STATUSES[type_]
    if status_ not in success | failure:
        failed, reason = True, f"unknown ack status {status_!r}"
    elif status_ in success and type_ in _ENTITY and not (isinstance(medusa_id, str) and medusa_id.strip()):
        failed, reason = True, "success ack without medusa_id"
    else:
        failed = status_ in failure
        if type_ == "inventory" and status_ == "skipped":   # it carries no reason: say what it means
            reason = "not applied by the consumer (skipped)"
    if type_ == "removed":
        return _ack_removed(ledger, external_id, failed, reason)
    if type_ == "inventory":
        return _ack_inventory(ledger, external_id, failed, reason, quantity, made_to_order)
    return _ack_entity(ledger, type_, external_id, medusa_id, failed, reason, payload_hash)


def _ack_entity(ledger: Ledger, type_: str, external_id: str, medusa_id: str | None,
                failed: bool, reason: str | None, payload_hash: str | None) -> int:
    """One variation/product ack (the lanes in _ENTITY). A failure counts an attempt and re-serves (dirty),
    dead-lettering to gap_held at SETTINGS.sync.max_attempts; a success records the medusa_id and marks the
    row synced. The eligibility, retire and lost-update guards below keep an out-of-order or stale ack from
    loading a product ahead of its variation, resurrecting a retiring row, or recording a version Medusa no
    longer holds. `failed`/`reason` are decided once in ack()."""
    table, pk = _ENTITY[type_]
    now = now_iso()
    if failed:
        row = ledger.execute(f"SELECT sync_attempts FROM {table} WHERE {pk} = ?",
                             (external_id,)).fetchone()
        attempts = ((row["sync_attempts"] if row else 0) or 0) + 1
        state = "gap_held" if attempts >= SETTINGS.sync.max_attempts else "dirty"   # dead-letter at the cap
        # same retire guard as the success path below: a stray/out-of-order FAILED ack must NOT knock a
        # 'retiring' row back to dirty/gap_held, which would re-enter the serve lane while its tombstone
        # still serves /removed. A failed ack on a retiring row is a no-op (0 rows) by design.
        cur = ledger.execute(
            f"UPDATE {table} SET state = ?, sync_attempts = ?, sync_error = ?, retry_after = ?, updated_at = ? "
            f"WHERE {pk} = ? AND state != 'retiring'",
            (state, attempts, (reason or "apply failed")[:500], _retry_after(attempts), now, external_id))
        if state == "gap_held" and cur.rowcount:
            log.warning("entity dead-lettered after repeated Medusa failures",
                        extra={"extra_fields": {"type": type_, "external_id": external_id,
                                                "attempts": attempts, "reason": reason}})
        return cur.rowcount
    # success: a PRODUCT is only marked synced if its variation is synced OR archived. An ack RECORDS what
    # Medusa did, so it must not re-apply the serve-time freeze rule: if the variety was archived between serve
    # and ack, refusing the ack would lose the id Medusa assigned and let withdraw_unsent orphan the product.
    # The VE2 frozen rule (no NEW product on an archived variety) lives once, in the serve predicate
    # (_ELIGIBLE_PRODUCT); a new product is never served, so it never reaches a success ack to begin with.
    guard = " AND variation_key IN (SELECT key FROM variation WHERE state = 'synced' OR archived = 1)" \
        if type_ == "products" else ""
    # retire guard: a success ack must NOT resurrect a row that is being RETIRED. A stray/out-of-order ack
    # would otherwise flip a 'retiring' row back to 'synced' while its variation tombstone still serves
    # /removed, diverging the two systems. (ack from pending/syncing/dirty stays allowed by design -- serve
    # leases are an optimization, not a precondition; only 'retiring' is protected.)
    # F3 lost-update guard: flip to synced only if what Medusa applied is STILL the current version. When
    # the ack echoes the served payload_hash (the robust round-trip), require it to equal the row's current
    # hash -- a produce that re-dirtied the row since serve stored a NEW hash, so this stale ack no longer
    # matches, is refused (0 rows), and the row re-serves the new content. Without the echoed hash (legacy
    # pull) the prior behavior is kept unchanged, so this is a backward-compatible opt-in: the guard
    # engages the moment the consumer starts echoing payload_hash, and nothing changes until it does.
    if payload_hash is not None:
        version_cond, version_params = " AND payload_hash = ?", [payload_hash]
    else:
        version_cond, version_params = "", []
    # reserve guard: a variation stamped reserved_at was reserved after this ack's lease (the next lease clears the
    # stamp), so Medusa applied what it held before the reserve. The ack records it and leaves the row dirty, and the
    # next pull serves it (sync.reserve_variations).
    synced = "CASE WHEN reserved_at IS NOT NULL THEN 'dirty' ELSE 'synced' END" if table == "variation" else "'synced'"
    cur = ledger.execute(
        f"UPDATE {table} SET medusa_id = ?, state = {synced}, sync_attempts = 0, sync_error = NULL, "
        f"retry_after = NULL, last_synced = ?, updated_at = ? WHERE {pk} = ? AND state != 'retiring'{guard}{version_cond}",
        (medusa_id, now, now, external_id, *version_params))
    if cur.rowcount == 0 and type_ == "products":
        log.warning("refused a product ack ahead of its variation (out-of-order / not resolvable)",
                    extra={"extra_fields": {"sku": external_id}})
    return cur.rowcount


def _ack_removed(ledger: Ledger, external_id: str, failed: bool, reason: str | None) -> int:
    """A tombstone ack. 'done' = Medusa deleted it -> retire the tombstone. 'blocked' (an open reservation blocks the
    hard-delete) or 'failed' -> keep it, count the attempt and wait (_block_tombstone). Returns the rows changed: 0
    for an unknown tombstone, or a 'done' the ledger refuses (below)."""
    row = ledger.execute("SELECT kind, reason, sync_attempts FROM removed WHERE external_id = ?",
                         (external_id,)).fetchone()
    if row is None:
        return 0
    if failed:
        return _block_tombstone(ledger, external_id, row["sync_attempts"], reason or "blocked")
    if row["kind"] == "variation":
        # E3 + self-heal: hard-delete the variety only once it is FK-safe (no product re-acquired it).
        # A raced produce with a stale retired-key exclusion could re-link a product onto the retiring Key; if so,
        # KEEP the tombstone and count the attempt like a block, so it re-serves after the wait (not on every
        # pull) and self-heals once the next (properly-excluding) produce moves those children off -- never a blind
        # DELETE that would FK-fail and orphan the row with no tombstone.
        child = ledger.execute("SELECT 1 FROM product WHERE variation_key = ? LIMIT 1", (external_id,)).fetchone()
        if child:
            log.warning("retiring variety re-acquired children; keeping its tombstone to re-serve",
                        extra={"extra_fields": {"key": external_id}})
            _block_tombstone(ledger, external_id, row["sync_attempts"],
                             "done refused: the variety has products in the ledger again")
            return 0
        ledger.execute("DELETE FROM variation WHERE key = ?", (external_id,))
        # table 575 / G5: remember the acked delete, so a stale live export never re-creates the variety
        ledger.upsert("variation_deleted", {"key": external_id, "reason": row["reason"], "deleted_at": now_iso()},
                      pk=("key",))
    return ledger.execute("DELETE FROM removed WHERE external_id = ?", (external_id,)).rowcount


def tombstoned_variations(ledger: Ledger) -> dict[str, tuple[str, str]]:
    """{Key: (tombstone, reason)} for every variety the scraper is deleting or Medusa has deleted: a variation tombstone
    in any state ('pending', or 'dead' after repeated blocks; the scraper's decision stands either way) and every acked
    delete ('done', variation_deleted). Emit's live-export union never re-adds these (table 575 / G5)."""
    out = {r["key"]: ("done", r["reason"] or "") for r in ledger.execute("SELECT key, reason FROM variation_deleted")}
    out.update({r["external_id"]: (r["state"], r["reason"] or "") for r in ledger.execute(
        "SELECT external_id, state, reason FROM removed WHERE kind = 'variation'")})
    return out


def _block_tombstone(ledger: Ledger, external_id: str, attempts_before: int, error: str) -> int:
    """Count one undelivered delete: the tombstone waits _retry_after, and after SETTINGS.sync.max_attempts it is
    'dead' so a permanently-reserved id stops re-serving (GET /failures lists it; a requeue serves it again)."""
    attempts = attempts_before + 1
    state = "dead" if attempts >= SETTINGS.sync.max_attempts else "pending"
    cur = ledger.execute(
        "UPDATE removed SET state = ?, sync_attempts = ?, sync_error = ?, retry_after = ?, updated_at = ? "
        "WHERE external_id = ?", (state, attempts, error[:500], _retry_after(attempts), now_iso(), external_id))
    if state == "dead" and cur.rowcount:
        log.warning("tombstone dead-lettered after repeated blocks (open reservation?)",
                    extra={"extra_fields": {"external_id": external_id, "attempts": attempts}})
    return cur.rowcount


def ack_batch(ledger: Ledger, acks: list[dict]) -> dict[str, int]:
    """Apply a batch of acks (POST /sync/ack body). Each: {type, external_id, medusa_id, status,
    error?}. Per-ack ISOLATED: a malformed/unsupported ack is skipped and logged, the rest still
    apply -- one bad ack never drops the batch. Returns {applied, missed, skipped}: applied changed a
    row (a failed attempt included: an unknown status or a success without its id is one); missed =
    external_id not found or ack refused (out-of-order, stale echo); skipped = malformed (an unknown lane)."""
    applied = missed = skipped = 0
    for a in acks:
        try:
            changed = ack(ledger, a["type"], a["external_id"], a.get("medusa_id"),
                          a.get("status", "synced"), a.get("error") or a.get("reason"),
                          payload_hash=a.get("payload_hash"), quantity=a.get("quantity"),
                          made_to_order=a.get("made_to_order"))
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
    # `state` in the item distinguishes an ACTIVE dead-letter (gap_held/dead, still surfaced for repair)
    # from one the operator ABANDONED (abandoned_at set, terminal): both render, so the Sync-failures view
    # can show the abandoned ones too, tagged so the UI can style them differently.
    def _row(type_, xid, state, attempts, error, updated, abandoned_at):
        return {"type": type_, "external_id": xid, "state": "abandoned" if abandoned_at else state,
                "abandoned": bool(abandoned_at), "attempts": attempts, "error": error,
                "updated_at": updated}
    for type_, table, active in (("variations", "variation", "gap_held"), ("products", "product", "gap_held")):
        pk = _ENTITY[type_][1]
        for r in ledger.execute(
            f"SELECT {pk} AS xid, state, sync_attempts, sync_error, updated_at, abandoned_at FROM {table} "
            f"WHERE state = ? OR abandoned_at IS NOT NULL ORDER BY updated_at DESC LIMIT ?", (active, limit)):
            out.append(_row(type_, r["xid"], r["state"], r["sync_attempts"], r["sync_error"],
                            r["updated_at"], r["abandoned_at"]))
    for r in ledger.execute(_PHOTO_HELD.replace("{cols}", "sku, sync_attempts, updated_at, photo_unpublished") +
                            " ORDER BY updated_at DESC LIMIT ?", (limit,)):   # held back: a photo not published
        files = json.loads(r["photo_unpublished"])
        out.append(_row("products", r["sku"], "photo_unpublished", r["sync_attempts"],
                        f"photo not published: {len(files)} files ({files[0]})", r["updated_at"], None))
    for r in ledger.execute(                             # tombstones stuck 'dead' (Medusa kept blocking)
        "SELECT external_id AS xid, state, sync_attempts, sync_error, updated_at, abandoned_at FROM removed "
        "WHERE state = 'dead' OR abandoned_at IS NOT NULL ORDER BY updated_at DESC LIMIT ?", (limit,)):
        out.append(_row("removed", r["xid"], r["state"], r["sync_attempts"], r["sync_error"],
                        r["updated_at"], r["abandoned_at"]))
    for r in ledger.execute(                             # stock Medusa kept failing or refusing
        "SELECT sku, sync_attempts, sync_error, updated_at, abandoned_at FROM inventory "
        "WHERE sync_attempts >= ? OR abandoned_at IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
            (SETTINGS.sync.max_attempts, limit)):
        out.append(_row("inventory", r["sku"], "dead", r["sync_attempts"], r["sync_error"], r["updated_at"],
                        r["abandoned_at"]))
    return out


# What a requeue resets, per lane (POST /sync/v1/requeue `type`; no type resets them all): a 'gap_held' variation or
# product back to 'dirty', a 'dead' tombstone back to 'pending', a dead stock row back to a plain delta. Each clears
# the counter, the wait AND the stale error; an abandoned dead-letter stays put. The one parameter is the time.
_REQUEUE_SQL = {
    **{lane: f"UPDATE {table} SET state = 'dirty', sync_attempts = 0, sync_error = NULL, retry_after = NULL, "
             "updated_at = ? WHERE state = 'gap_held' AND abandoned_at IS NULL"
       for lane, (table, _pk) in _ENTITY.items()},
    "removed": "UPDATE removed SET state = 'pending', sync_attempts = 0, sync_error = NULL, retry_after = NULL, "
               "updated_at = ? WHERE state = 'dead' AND abandoned_at IS NULL",
    "inventory": f"UPDATE inventory AS i SET {CLEAR_STOCK_FAILURES_SQL}, updated_at = ? WHERE {_STOCK_DEAD}",
}
REQUEUE_TYPES = tuple(_REQUEUE_SQL)


def requeue_dead_lettered(ledger: Ledger, type_: str | None = None) -> int:
    """Recovery: reset dead-lettered entities (_REQUEUE_SQL) so they are re-served at once -- used after the
    underlying Medusa issue is fixed (e.g. a transient outage that hit the cap). Returns the number requeued.
    type_ None = every lane in REQUEUE_TYPES."""
    if type_ is not None and type_ not in REQUEUE_TYPES:
        raise ValueError(f"unsupported requeue type {type_!r}; expected one of {REQUEUE_TYPES}")
    now = now_iso()
    return sum(ledger.execute(_REQUEUE_SQL[lane], (now,)).rowcount for lane in ([type_] if type_ else REQUEUE_TYPES))


# Why the scraper does not serve an existing variation, the first that applies (NULL = it serves). The reserve answers
# it as `frozen` and never marks the row. The operator's terminal verdict (an abandoned dead-letter) outranks the rest;
# then the gates of _ELIGIBLE_VARIATION, save the retry wait, which only delays. 'uncomputed' comes LAST (Mas, 11-12):
# its alias_rows or codes are NULL, not computed yet (ledger v18), and the remedy is to run a produce.
_FROZEN_REASON_SQL = ("CASE WHEN abandoned_at IS NOT NULL THEN 'abandoned' "
                      "WHEN archived = 1 THEN 'archived' WHEN state = 'retiring' THEN 'retiring' "
                      "WHEN in_full = 0 THEN 'not_in_full' WHEN type IS NULL OR type = '' THEN 'no_type' "
                      "WHEN alias_rows IS NULL OR codes IS NULL THEN 'uncomputed' END")


def reserve_variations(ledger: Ledger, external_ids: Iterable[str]) -> dict[str, list]:
    """POST /sync/v1/reserve: re-serve exactly these variation Keys once, with their whole payload (the backend's
    variety restore, 173 F18). Each deduped Key lands in exactly one list, sorted by Key:
    - `unknown`: no ledger row; never created.
    - `frozen`: {external_id, reason}, the row exists but the scraper does not serve it (_FROZEN_REASON_SQL);
      never marked.
    - `reserved`: every other row. It WILL serve on a coming pull (a retry wait only delays it): marked and stamped
      by RESERVE_SQL, so it outranks every lease issued before it.
    Idempotent: a stamped row is skipped, so a second call before the next pull writes nothing. The bound
    (SETTINGS.sync.max_reserve_ids) is the route's to enforce, before any write."""
    keys = sorted(set(external_ids))
    marks = ",".join("?" * len(keys))
    now = now_iso()
    # the write first: it opens the transaction, so the read below classifies the same rows the write marked
    ledger.execute(f"UPDATE variation SET {RESERVE_SQL}, updated_at = ? WHERE key IN ({marks}) "
                   f"AND ({_FROZEN_REASON_SQL}) IS NULL AND reserved_at IS NULL", (now, now, *keys))
    reason = {r["key"]: r["reason"] for r in ledger.execute(
        f"SELECT key, {_FROZEN_REASON_SQL} AS reason FROM variation WHERE key IN ({marks})", keys)}
    return {"reserved": [k for k in keys if k in reason and reason[k] is None],
            "unknown": [k for k in keys if k not in reason],
            "frozen": [{"external_id": k, "reason": reason[k]} for k in keys if reason.get(k)]}


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
    # abandoned_at is cleared on a re-serve-from-zero reset: the reset IS the un-abandon escape hatch, so a
    # dropped dead-letter gets one more chance (it re-serves; if it still structurally fails it re-dead-letters).
    sets += [f"{c} = CASE WHEN state = 'retiring' THEN {c} ELSE NULL END"
             for c in ("medusa_id", "last_synced", "sync_error", "abandoned_at", "retry_after") if c in cols]
    if "sync_attempts" in cols:
        sets.append("sync_attempts = CASE WHEN state = 'retiring' THEN sync_attempts ELSE 0 END")
    return ledger.execute(f"UPDATE {table} SET {', '.join(sets)}{where}", (now, *params)).rowcount


def reconcile_variations_to_seed(ledger: Ledger, seed_keys: set[str], protected: set[str] | None = None,
                                 seed_identities: set[tuple] | None = None) -> dict:
    """Make the variation table dup-free and seed-consistent -- the missing half of a TRUE cold start.

    A pristine reset zeroes sync state but by its old invariant never removed a live variety, so a
    duplicate / re-key OLD SIDE seeded before a seed cleanup lingers forever and every produce re-renders
    it (the ledger is the render source). This collapses every (type, name) that resolves to MORE
    THAN ONE Key to a single survivor -- the committed-seed Key if one is present, else the clean-slug Key
    -- and TOMBSTONES + drops the rest on the ONE removed lane (kind='variation'). The tombstone is what
    makes Medusa delete the old side by Key on the removals pull; Medusa's own consumer blocks a variety
    that still has products, so a live one can never be wrongly deleted. A minted variety with no twin is
    untouched (it re-mints deterministically). Global-only. Returns counts + the dropped Keys.

    `seed_identities` (the (type, name) identity set of the committed seed): when given, a variety
    whose identity is NOT in the seed -- a test mint or a Medusa-only leftover carried back by the export
    union -- is dropped WHOLE (all its category Keys) and tombstoned, so a factory reset + removals pull
    brings Medusa in line with the base instead of re-importing the leftover forever. Identity-keyed, so two
    Keys of the SAME variety match and a real variety is never dropped for a key mismatch. Omit it (None) to keep the old dedup-only behaviour.

    `protected` is the operator's 'not a duplicate' set (decisions_store.protected_keys): a Key in it is
    NEVER dropped, so a false-positive collapse the operator already overrode does not re-tombstone on the
    next reconcile. It also can never be the sole reason a survivor is lost -- a protected Key just stays.

    Uses the SAME variety_identity + survivor rule as the emit dedup gate (loaders), so the ledger, the
    matcher reference, and the emitted catalog all agree on exactly one Key per variety."""
    import collections
    from stone_pipeline.reference.loaders import survivor_of, variety_identity
    # the write lock first, as purge does: an ack committing between the product SELECT below and the deletes could
    # otherwise give a product a medusa_id after it was read as unsent, and it would be deleted without a tombstone
    _lock_and_check_in_flight(ledger)
    protected = protected or set()
    groups: dict[tuple, list[str]] = collections.defaultdict(list)
    for v in ledger.execute("SELECT key, name FROM variation").fetchall():
        groups[variety_identity(v["key"], v["name"])].append(v["key"])
    dedup: list[str] = []           # dup-losers within a seed variety (keep the survivor)
    stale: list[str] = []           # varieties whose identity is not in the committed seed (drop whole)
    for ident, keys in groups.items():
        if seed_identities is not None and ident not in seed_identities:
            stale += [k for k in keys if k not in protected]
            continue
        if len(keys) <= 1:
            continue
        survivor = survivor_of(keys, seed_keys)
        dedup += [k for k in keys if k != survivor and k not in protected]
    # a variety a kept record shows (a gone or delisted listing, db.KEPT_SKUS_SQL) stays: that record is never deleted
    # (table 505), and the variety is reported
    kept_keys = {r["variation_key"] for r in ledger.execute(
        f"SELECT DISTINCT variation_key FROM product WHERE sku IN ({KEPT_SKUS_SQL})")}
    varieties_kept = sorted(k for k in dedup + stale if k in kept_keys)
    dedup = [k for k in dedup if k not in kept_keys]
    stale = [k for k in stale if k not in kept_keys]
    dropped = dedup + stale
    if dropped:
        for keys, reason in ((dedup, "reseed_dedup"), (stale, "not_in_seed")):
            if not keys:
                continue
            # the products of a dropped variety are deleted below: a product Medusa holds is tombstoned first (as a
            # hard reset does), so Medusa deletes it too instead of keeping it as an orphan
            q = ",".join("?" * len(keys))
            held = ledger.execute(f"SELECT p.sku, p.source FROM product p WHERE p.variation_key IN ({q}) "
                                  f"AND {in_medusa_sql('p')}", tuple(keys)).fetchall()
            record_tombstones(ledger, [(r["sku"], r["source"]) for r in held], reason=reason, kind="product")
            record_tombstones(ledger, [(k, None) for k in keys], reason=reason, kind="variation")
        q = ",".join("?" * len(dropped))
        # FK order: inventory -> product -> variation (its icons cascade). The reconcile runs before the reset, so
        # a dropped variety can still have products here.
        ledger.execute(f"DELETE FROM inventory WHERE sku IN "
                       f"(SELECT sku FROM product WHERE variation_key IN ({q}))", tuple(dropped))
        ledger.execute(f"DELETE FROM product WHERE variation_key IN ({q})", tuple(dropped))
        ledger.execute(f"DELETE FROM variation WHERE key IN ({q})", tuple(dropped))
    log.warning("variation table reconciled to seed (dedup old sides + drop non-seed leftovers)",
                extra={"extra_fields": {"tombstoned_dropped": len(dropped), "dedup": len(dedup),
                                        "not_in_seed": len(stale), "varieties_kept": varieties_kept}})
    return {"tombstoned_dropped": len(dropped), "dedup": len(dedup),
            "not_in_seed": len(stale), "dropped_keys": dropped, "varieties_kept": varieties_kept}


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
    (variations/attributes) is reset only on a GLOBAL (unscoped) reset. Returns row counts.
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

    # Attribute ids (category AND color/finish/quality/type) are pre-existing Medusa identity from
    # attributes.csv, NOT scraper-assigned sync state; products reverse-map every one of them through the
    # attribute table at populate/render time. The overlay reset above nulls them ALL, which would leave
    # products with empty attributes (and, for category, wedge the pull), and open_ledger only re-seeds on
    # an EMPTY variation table -- which a non-hard reset never leaves it. Restore all of them from the
    # authoritative export immediately (global reset only). See bootstrap.restore_attribute_ids.
    if not scoped:
        from stone_pipeline.ledger.bootstrap import restore_attribute_ids
        out["attribute_ids_restored"] = restore_attribute_ids(ledger)

    if hard:                                   # drop the scraper output; re-scrape rebuilds it
        # TOMBSTONE every product Medusa holds (db.in_medusa_sql) before deleting it, so Medusa deletes it too. Without
        # this a hard / factory reset drops products locally but never tells Medusa, and its upsert-only catalogue
        # pull leaves them as orphans forever. A product Medusa never received is dropped silently. Mirrors
        # retire_variation's E2 synced-only rule.
        # the SELECT already has a WHERE, so fold a scoped source filter in as AND (not a second WHERE)
        # A kept record (a gone or delisted listing, db.KEPT_SKUS_SQL) is spared: it is never deleted (table 505), and
        # no re-scrape brings it back.
        src_and = where.replace(" WHERE ", " AND ", 1) if scoped else ""
        not_kept = f"sku NOT IN ({KEPT_SKUS_SQL})"
        out["records_kept"] = ledger.execute(
            f"SELECT COUNT(*) FROM product WHERE sku IN ({KEPT_SKUS_SQL}){src_and}", params).fetchone()[0]
        synced = ledger.execute(f"SELECT p.sku, p.source FROM product p WHERE {in_medusa_sql('p')}{src_and} "
                                f"AND p.{not_kept}", params).fetchall()
        if synced:
            record_tombstones(ledger, [(r["sku"], r["source"]) for r in synced],
                              reason="reset_dropped", kind="product")
        out["product_tombstoned"] = len(synced)
        out["inventory"] = ledger.execute(
            f"DELETE FROM inventory WHERE {not_kept} AND sku IN (SELECT sku FROM product{where})",
            params).rowcount if scoped else ledger.execute(f"DELETE FROM inventory WHERE {not_kept}").rowcount
        out["product"] = ledger.execute(
            f"DELETE FROM product{where}{' AND ' if where else ' WHERE '}{not_kept}", params).rowcount
        # the spared records re-serve once from zero, as a soft reset does: a coordinated factory reset wipes Medusa
        # too, so a record kept with its old sync overlay would never be sent again
        kept_scope = f"{where}{' AND ' if where else ' WHERE '}sku IN ({KEPT_SKUS_SQL})"
        _reset_overlay(ledger, "product", now, kept_scope, params)
        ledger.execute(f"UPDATE inventory SET {FORGET_SYNCED_STOCK_SQL}, updated_at = ? WHERE sku IN ({KEPT_SKUS_SQL})"
                       + (f" AND sku IN (SELECT sku FROM product{where})" if scoped else ""), (now, *params))
    else:                                      # keep the rows, just re-serve them from zero
        out["product"] = _reset_overlay(ledger, "product", now, where, params)
        inv_where = " WHERE sku IN (SELECT sku FROM product%s)" % where if scoped else ""
        out["inventory"] = ledger.execute(
            f"UPDATE inventory SET {FORGET_SYNCED_STOCK_SQL}, updated_at = ?{inv_where}",
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
                                  "sync_error": None, "retry_after": None, "created_at": now, "updated_at": now},
                      pk=("external_id",))
    return len(pairs)


def purge_discontinued(ledger: Ledger, source_codes: list[str] | None = None) -> dict:
    """Hard-delete the bundles their lot replaced (stock 0, reason 'replaced_by_lot') and their inventory rows: the
    only dead stock. Every other stock-0 product stays: a listing gone at its supplier stays published as sold out or
    keeps being hidden, and a delisted vendor's products are hidden with their records kept; their records are never
    deleted (owner 11-11, table 505). Variation/backbone rows are untouched. `source_codes` scopes to those sources.
    Records a tombstone per deleted SKU (reason 'discontinued') so Medusa deletes the same set (product +
    scraper_sync_ref) via the pull lane. Returns {product, inventory, external_ids}."""
    _lock_and_check_in_flight(ledger)          # same atomic guard as reset -- never purge mid-serve
    scope = ""
    params: tuple = ()
    if source_codes is not None:
        marks = ",".join("?" * len(source_codes)) or "NULL"
        scope, params = (f" AND p.source IN ({marks})", tuple(source_codes))
    rows = ledger.execute(
        f"SELECT p.sku AS sku, p.source AS source FROM product p JOIN inventory i ON i.sku = p.sku "
        f"WHERE i.reason = 'replaced_by_lot' AND NOT {on_sale_sql('i')}{scope}", params).fetchall()
    skus = [r["sku"] for r in rows]
    if not skus:
        return {"product": 0, "inventory": 0, "external_ids": []}
    record_tombstones(ledger, [(r["sku"], r["source"]) for r in rows], "discontinued")   # deliver the delete
    q = ",".join("?" * len(skus))
    inv = ledger.execute(f"DELETE FROM inventory WHERE sku IN ({q})", tuple(skus)).rowcount   # child first (FK)
    prod = ledger.execute(f"DELETE FROM product WHERE sku IN ({q})", tuple(skus)).rowcount
    log.warning("ledger PURGE replaced bundles", extra={"extra_fields": {
        "sources": source_codes or "all", "product": prod, "inventory": inv}})
    return {"product": prod, "inventory": inv, "external_ids": skus}


def unpublish_source(ledger: Ledger, source_codes: list[str]) -> dict:
    """Stage 2 of a vendor removal ('Remove permanently'): hide EVERY product of the sources (stock 0, reason
    'delisted'), sold-out and hidden ones included, so Blokport unpublishes them. Records, gone_at, photos and textures
    stay; no tombstone, since a tombstone deletes the backend record and its image rows, which renders and the bespoke
    library read (table 505, agreed with M4). Idempotent: a product already delisted is left as it is. Returns
    {unpublished, external_ids}."""
    _lock_and_check_in_flight(ledger)          # same atomic guard as reset/purge -- never mutate mid-serve
    marks = ",".join("?" * len(source_codes)) or "NULL"
    skus = [r["sku"] for r in ledger.execute(
        f"SELECT p.sku FROM product p LEFT JOIN inventory i ON i.sku = p.sku "
        f"WHERE p.source IN ({marks}) AND i.reason IS NOT 'delisted' ORDER BY p.sku", tuple(source_codes))]
    now = now_iso()
    for sku in skus:                           # a product with no stock row yet gets one: it must be hidden too
        ledger.upsert("inventory", {"sku": sku, "qty": 0, "made_to_order": 0, "reason": "delisted",
                                    "updated_at": now}, pk=("sku",))
    if skus:
        q = ",".join("?" * len(skus))
        ledger.execute(f"UPDATE inventory SET {CLEAR_STOCK_FAILURES_SQL} WHERE sku IN ({q})", tuple(skus))
    log.warning("ledger UNPUBLISH source (vendor removal)", extra={"extra_fields": {
        "sources": source_codes, "unpublished": len(skus)}})
    return {"unpublished": len(skus), "external_ids": skus}


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
        f"SELECT p.sku FROM product p JOIN inventory i ON i.sku = p.sku WHERE {on_sale_sql('i')}{scope}", params)]
    if not skus:
        return {"delisted": 0, "external_ids": []}
    q = ",".join("?" * len(skus))
    # reason='delisted' rides the inventory delta so Medusa HIDES exactly these SKUs, provenance-safe, and keeps
    # their records (table 505).
    ledger.execute(f"UPDATE inventory SET qty = 0, made_to_order = 0, reason = 'delisted', "   # a new stock: no wait
                   f"{CLEAR_STOCK_FAILURES_SQL}, updated_at = ? WHERE sku IN ({q})",
                   (now_iso(), *skus))
    log.warning("ledger DELIST source (qty -> 0)", extra={"extra_fields": {
        "sources": source_codes or "all", "delisted": len(skus)}})
    return {"delisted": len(skus), "external_ids": skus}


def kept_photos(ledger: Ledger) -> dict[str, list[str]]:
    """{source: sorted shas} of every photo a kept record links (db.KEPT_SKUS_SQL: a listing gone at its supplier or
    delisted, the same records a reset spares; table 505). Read from its image links (thumbnail, product and oriented
    images), whatever their folder (imagestore.photo_of), so a photo's views, its 3D slab and a tile listing's rendered
    tile all count. A link list that does not read fails loud: a photo left out would be deleted."""
    from stone_pipeline.io import imagestore
    kept: dict[str, set[str]] = {}
    for r in ledger.execute(
            "SELECT p.thumbnail_key AS thumb, p.product_image_keys AS product, p.oriented_image_keys AS oriented "
            f"FROM product p WHERE p.sku IN ({KEPT_SKUS_SQL})"):
        for link in (r["thumb"], *json.loads(r["product"] or "[]"), *json.loads(r["oriented"] or "[]")):
            if photo := imagestore.photo_of(link or ""):
                kept.setdefault(photo[0], set()).add(photo[1])
    return {source: sorted(shas) for source, shas in sorted(kept.items())}


def live_products(ledger: Ledger, source_codes: list[str] | None = None) -> int:
    """How many of a source's products are still LIVE (on sale, db.on_sale_sql). The vendor-removal DELETE
    guard uses this: a source with live products must be taken offline (delisted to qty 0, pulled by
    Medusa) before it can be removed, so nothing is left orphaned-live in the shop."""
    scope, params = "", ()
    if source_codes is not None:
        marks = ",".join("?" * len(source_codes)) or "NULL"
        scope, params = (f" AND p.source IN ({marks})", tuple(source_codes))
    row = ledger.execute(
        f"SELECT COUNT(*) AS n FROM product p JOIN inventory i ON i.sku = p.sku WHERE {on_sale_sql('i')}{scope}",
        params).fetchone()
    return int(row["n"]) if row else 0


def variation_live_products(ledger: Ledger, key: str) -> int:
    """How many LIVE (on sale, db.on_sale_sql) products a variety still has. retire_variation guards on this (E11): a
    variety with live products must have them delisted/moved first, or `force` cascades them."""
    row = ledger.execute("SELECT COUNT(*) AS n FROM product p JOIN inventory i ON i.sku = p.sku "
                         f"WHERE p.variation_key = ? AND {on_sale_sql('i')}", (key,)).fetchone()
    return int(row["n"]) if row else 0


class KeptListingsShowVariety(RuntimeError):
    """A variety some kept record shows (a gone or delisted listing, db.KEPT_SKUS_SQL) cannot be retired: retiring
    cascades its products, and a kept record is never deleted (table 505), the one rule a reset follows too. Raised
    whatever `force` says: the operator moves or decides those listings first."""


def variation_kept_listings(ledger: Ledger, key: str) -> list[str]:
    """The SKUs of the kept records (db.KEPT_SKUS_SQL) that show this variety, sorted."""
    return [r["sku"] for r in ledger.execute(
        f"SELECT sku FROM product WHERE variation_key = ? AND sku IN ({KEPT_SKUS_SQL}) ORDER BY sku", (key,))]


def retire_variation(ledger: Ledger, key: str, force: bool = False,
                     reason: str = "variation_removed") -> dict:
    """Cascade a variety's explicit removal (also the old side of a re-key). Its products (which belong to
    it) are tombstoned + hard-deleted, so the variety row is FK-safe to drop; then EITHER a variation tombstone is recorded and the row held 'retiring' until
    Medusa acks the delete (only if it was ever synced -- E2), OR the row is dropped locally now (a
    never-synced variety: Medusa never received it, so there is nothing to tell it to delete). The caller
    (lifecycle) has already guarded existence + the live-product precondition. Serve-in-flight guarded."""
    _lock_and_check_in_flight(ledger)
    if kept := variation_kept_listings(ledger, key):
        raise KeptListingsShowVariety(f"variety {key!r} is shown by {len(kept)} gone or delisted listing(s) "
                                      f"{', '.join(kept)}: their records stay (table 505); move them to another "
                                      "variety first")
    var = ledger.get("variation", "key", key)
    prods = ledger.execute("SELECT sku, source FROM product WHERE variation_key = ?", (key,)).fetchall()
    if prods:                                  # E1: product tombstones (serve before the variation one)
        record_tombstones(ledger, [(p["sku"], p["source"]) for p in prods],
                          reason="vendor_removed", kind="product")
        skus = [p["sku"] for p in prods]
        q = ",".join("?" * len(skus))
        ledger.execute(f"DELETE FROM inventory WHERE sku IN ({q})", tuple(skus))   # child first (FK)
        ledger.execute(f"DELETE FROM product WHERE sku IN ({q})", tuple(skus))
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


def cancel_variation_tombstone(ledger: Ledger, key: str) -> dict:
    """'Not a duplicate': cancel a pending variation tombstone that a dedup/reconcile raised in error, so
    Medusa does NOT delete this variety. Drops the pending removed row and, if the variation is still held
    'retiring', flips it back to serving. Unlike un_retire it does NOT clear the retired-exclusion memory
    (the variety was never operator-retired -- it was auto-tombstoned), and the CALLER records the durable
    'protected' verdict so a future reconcile never re-drops it. Idempotent: a repeat clears 0 and returns
    the current state. 404 (via caller) only if the key is unknown AND has no tombstone to cancel."""
    _lock_and_check_in_flight(ledger)
    var = ledger.get("variation", "key", key)
    restored = False
    if var is not None and var["state"] == "retiring":
        ledger.set_state("variation", "key", key, "dirty" if var["medusa_id"] else "pending")
        restored = True
    cleared = ledger.execute("DELETE FROM removed WHERE kind = 'variation' AND external_id = ?",
                             (key,)).rowcount
    return {"key": key, "tombstone_cleared": cleared, "restored": restored,
            "known": var is not None}


# The dead-letters an operator may abandon, per failures `type`: (table, pk, the dead predicate, its label).
_ABANDONABLE = {
    "variations": ("variation", "key", "state = 'gap_held'", "gap_held"),
    "products": ("product", "sku", "state = 'gap_held'", "gap_held"),
    "removed": ("removed", "external_id", "state = 'dead'", "dead"),
    "inventory": ("inventory", "sku", f"sync_attempts >= {SETTINGS.sync.max_attempts}", "dead"),
}


def abandon_dead_letter(ledger: Ledger, type_: str, external_id: str) -> dict:
    """Drop ONE dead-letter that structurally re-rejects on Requeue: stamp `abandoned_at` so it stops
    re-serving AND Requeue no longer resurrects it, WITHOUT a blind DELETE (the row + its last error stay
    auditable, and /failures still renders it, tagged abandoned). Terminal until a reset (or, for a variation,
    product or stock row, new content) clears it. type_ is any GET /failures `type` (_ABANDONABLE). Idempotent: an
    already-abandoned id changes 0 rows and reports it. Serve-safe (it only touches a NON-served dead-letter row),
    so -- like requeue -- it takes no in-flight-pull lock."""
    if type_ not in _ABANDONABLE:
        return {"error": f"unknown type {type_!r}; expected one of {sorted(_ABANDONABLE)}"}
    table, pk, dead, label = _ABANDONABLE[type_]
    row = ledger.execute(f"SELECT ({dead}) AS dead, abandoned_at FROM {table} WHERE {pk} = ?",
                         (external_id,)).fetchone()
    if row is None:
        return {"type": type_, "external_id": external_id, "abandoned": False, "found": False}
    if row["abandoned_at"]:
        return {"type": type_, "external_id": external_id, "abandoned": True, "already": True}
    if not row["dead"]:
        # only a dead-lettered row may be abandoned: refuse to strand a live/serving entity
        return {"type": type_, "external_id": external_id, "abandoned": False,
                "error": f"not a dead-letter (not {label})"}
    now = now_iso()
    ledger.execute(f"UPDATE {table} SET abandoned_at = ?, updated_at = ? WHERE {pk} = ?", (now, now, external_id))
    return {"type": type_, "external_id": external_id, "abandoned": True, "was": label}


def main(argv: list[str] | None = None) -> int:
    import json as _json

    from stone_pipeline.ledger import writethrough

    path = writethrough.ledger_path()
    if not path.exists():
        print(f"no ledger at {path} (run with SCRAPER_LEDGER_WRITETHROUGH=1 first)")
        return 1
    with writethrough.connect(path, bind=True) as ledger:
        print(_json.dumps(status(ledger), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
