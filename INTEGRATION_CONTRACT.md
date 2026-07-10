# Frozen response contracts (scraper side) for the :4200 integration

Exact shapes for the three endpoints the Blokport review flagged (S-1..S-4), verified against the actual
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

Item (now pinned; `updated_at` added for the drill-down):
```
{ "type": "variations" | "products" | "removed",
  "external_id": "<str>",     // variations -> variation Key; products -> SKU; removed -> tombstone external_id
  "attempts": <int>,          // sync_attempts (dead-lettered at the cap)
  "error": "<str>" | null,    // last sync_error text (may be null)
  "updated_at": "<ISO8601>" } // last-attempt timestamp
```
Ordered newest-first, capped at `limit` (default 200, via `?limit=`). `variations`/`products` items are rows
in state `gap_held`; `removed` items are tombstones stuck in state `dead`.

## S-3 -- GET /sync/v1/status  (sync server :8723)  -- READ THE GOTCHAS

Response:
```
{ "attribute":   { "<state>": <count>, ... },
  "variation":   { "<state>": <count>, ... },   // gap_held appears HERE
  "combination": { ... },
  "product":     { "<state>": <count>, ... },   // gap_held appears HERE
  "gap":         { ... },
  "removed":     { "<state>": <count>, ... },   // dead-letters are state 'dead' HERE (NOT gap_held)
  "inventory":   { "total": <int>, "delta": <int> },   // NOT a state histogram: NO gap_held key
  "variation_reconcile": { "orphaned": <int> } }
```

Three things that make `status.scraper[type].gap_held` read wrong if assumed:
1. **`gap_held` is OMITTED when zero.** Each per-table map is a `GROUP BY state` histogram, so a state with
   0 rows is ABSENT, not `0`. `status.variation.gap_held` is `undefined` when there are none -- read it as
   `?? 0`.
2. **`gap_held` lives ONLY under `variation` and `product`** (the lanes the ack fail-path dead-letters).
   `inventory` has no states (it is `{total, delta}`) -- there is no `inventory.gap_held` to sum.
3. **`removed` dead-letters use state `dead`, not `gap_held`.** They appear in `/failures` (type=`removed`)
   and in `status.removed.dead`, but NOT in any `gap_held` bucket. So the failures LIST can be non-empty
   while `(variation.gap_held ?? 0) + (product.gap_held ?? 0)` is 0 -- exactly the "list has items but the
   badge is 0" case. When the drill-down is open, take the count from the failures list length, not the
   gap_held sum. A full dead-letter badge = `(variation.gap_held ?? 0) + (product.gap_held ?? 0) +
   (removed.dead ?? 0)`.

## Notes
- `combinations` is NOT a sync lane (dormant): it never appears in `/failures` and its `status.combination`
  histogram is not part of the dead-letter total.
- These shapes are stable; a change here is a versioned contract change, not silent drift.
