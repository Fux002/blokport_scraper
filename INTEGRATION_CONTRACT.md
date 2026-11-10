# Frozen response contracts (scraper side) for the :4200 integration

Exact shapes for the endpoints the Blokport integration binds to (S-1..S-5), verified against the actual
response-builders (`ledger/sync.py`, `lifecycle.py`), not assumptions. Bind to THESE field names exactly and
fail loud on any mismatch. The full endpoint list is in `FRONTEND_API.md`; this pins the shapes.

## S-1 -- POST /config/v1/purge  (config server :8724)

Scoping: accepts `{ "sources": ["<source_name>", ...] }`. **Confirmed scoped.** Omit / `null` / absent =
ALL sources.

Response `200` (from `sync.purge_discontinued`):
```
{ "product": <int>,          // count of product rows hard-deleted
  "inventory": <int>,        // count of inventory rows hard-deleted
  "external_ids": ["<SKU>", ...] }   // FLAT array of the deleted product SKUs (NOT per-source)
```
- `external_ids` is a flat `string[]` of product SKUs -- your assumption is correct.
- Deleted count IS returned: `product` == `len(external_ids)`. Cross-check `purged N vs matched M` against
  `product` (or `external_ids.length`).
- Nothing to purge (no qty-0 dead stock): `{ "product": 0, "inventory": 0, "external_ids": [] }`.
- `409` if a sync pull is in flight (never purge mid-serve) -- body `{ "error": "..." }`.
- Semantics: each SKU is tombstoned so Medusa deletes the same set (product + scraper_sync_ref) via the
  `/sync/v1/removed` pull lane.

## S-4 -- inventory keying (answers B-2's investigation)

`inventory.sku == product.sku` -- the purge JOIN is `inventory i ON i.sku = p.sku`. So the purge's
`external_ids` (product SKUs) ARE exactly the inventory keys. **Blokport clears the matching inventory refs
by the SAME SKU** -- no separate inventory-id list, no inference. There is no variant-scoping or prefixing on
the ledger side: one SKU addresses both the product and its inventory row.

## S-2 -- GET /sync/v1/failures  (sync server :8723)

Envelope: `{ "failures": [ <item>, ... ] }`.

Item (`ledger/sync.py` `failures`):
```
{ "type": "variations" | "products" | "removed" | "inventory",
  "external_id": "<str>",     // variations -> variation Key; products and inventory -> SKU; removed -> tombstone external_id
  "state": "<str>",           // the row's dead-letter state (below), or "abandoned" once abandoned
  "abandoned": <bool>,        // true once POST /sync/v1/abandon (S-5) dropped it; terminal until a reset or new content
  "attempts": <int>,          // sync_attempts (dead-lettered at the cap)
  "error": "<str>" | null,    // last sync_error text (may be null)
  "updated_at": "<ISO8601>" } // last-attempt timestamp
```
`state` per `type`: `variations`/`products` rows in state `gap_held`; `removed` tombstones stuck in state `dead`;
`inventory` stock rows at the attempt cap, state `dead`. Abandoned rows of every type are listed too, with
`state` `"abandoned"`. The lanes come in that order (variations, products, removed, inventory), each newest-first
and each capped at `limit` (default 200, via `?limit=`), so the list can hold up to four times `limit` items.

## S-3 -- GET /sync/v1/status  (sync server :8723)  -- READ THE GOTCHAS

Response:
```
{ "attribute":   { "<state>": <count>, ... },
  "variation":   { "<state>": <count>, ... },   // gap_held appears HERE
  "combination": { ... },
  "product":     { "<state>": <count>, ... },   // gap_held appears HERE
  "gap":         { ... },
  "removed":     { "<state>": <count>, ... },   // dead-letters are state 'dead' HERE (NOT gap_held)
  "inventory":   { "total": <int>, "delta": <int>, "dead": <int>, "waiting": <int> },   // NOT a state histogram: NO gap_held key
  "variation_reconcile": { "orphaned": <int> } }   // orphaned: DEPRECATED (C13), to be removed; seed-inclusive, not actionable -- use variation.retiring
```

Three things that make `status.scraper[type].gap_held` read wrong if assumed:
1. **Every state is present, `0` when it holds none** (since 2026-11-05; before, a state with 0 rows was
   ABSENT). Each per-table map lists every state its table can hold (`db.STATES`, pinned to schema.sql's
   CHECK), so `status.variation.gap_held` is `0` when there are none. A reader's `?? 0` stays correct.
2. **`gap_held` lives ONLY under `variation` and `product`** (the lanes the ack fail-path dead-letters).
   `inventory` has no states (it is `{total, delta, dead, waiting}`: `delta` = what the inventory lane would
   serve now, `dead` = stock rows at the attempt cap and not abandoned, `waiting` = un-hides waiting on their
   product's own sync) -- there is no `inventory.gap_held` to sum.
3. **`removed` dead-letters use state `dead`, not `gap_held`.** They appear in `/failures` (type=`removed`)
   and in `status.removed.dead`, but NOT in any `gap_held` bucket. So the failures LIST can be non-empty
   while `(variation.gap_held ?? 0) + (product.gap_held ?? 0)` is 0 -- exactly the "list has items but the
   badge is 0" case. When the drill-down is open, take the count from the failures list length, not the
   gap_held sum. A full dead-letter badge = `(variation.gap_held ?? 0) + (product.gap_held ?? 0) +
   (removed.dead ?? 0) + (inventory.dead ?? 0)`.

## S-5 -- POST /sync/v1/abandon  (sync server :8723)

Drops ONE dead-letter to terminal: it stops re-serving and a requeue no longer revives it; the row stays and
`/failures` (S-2) lists it with `state` `"abandoned"`. A reset clears it, and so does new content for a variation,
product or stock row. Keyed by the same `{type, external_id}` an S-2 item carries, so a per-row Abandon button passes
them straight through (`ledger/server.py` `dispatch`, `ledger/sync.py` `abandon_dead_letter`).

Body: `{ "type": "variations" | "products" | "removed" | "inventory", "external_id": "<str>" }`.
A row is abandonable only in its dead-letter state: `variations`/`products` in `gap_held`, `removed` in `dead`,
`inventory` at the attempt cap.

| Status | When | Body |
|---|---|---|
| `200` | abandoned now | `{ "type", "external_id", "abandoned": true, "was": "gap_held" \| "dead" }` |
| `200` | already abandoned (idempotent, nothing written) | `{ "type", "external_id", "abandoned": true, "already": true }` |
| `400` | `type` or `external_id` missing | `{ "error": "abandon body must be {type, external_id}" }` |
| `400` | `type` not one of the four | `{ "error": "unknown type '<t>'; expected one of [...]" }` |
| `404` | no such row | `{ "error": "unknown <type> '<external_id>'" }` |
| `409` | the row exists but is not a dead-letter (still live or serving); nothing written | `{ "type", "external_id", "abandoned": false, "error": "not a dead-letter (not <state>)" }` |

`abandoned` is a boolean in every body. A `4xx` is a refusal, never a `200`.

## Notes
- `combinations` is NOT a sync lane (dormant): it never appears in `/failures` and its `status.combination`
  histogram is not part of the dead-letter total.
- These shapes are stable; a change here is a versioned contract change, not silent drift.
