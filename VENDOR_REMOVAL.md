# Vendor removal and re-add

How a vendor (source) is taken offline, removed, or re-added, end to end across the
scraper and Medusa. This is the confirmed contract both sides build against. It is a
companion to SYNC_LEDGER_DESIGN.md (the pull ledger it extends). No em dashes, per
that document's convention.

## Principle

The scraper never writes to Medusa. Medusa pulls read-only from the scraper ledger
and acks back. Vendor removal is therefore expressed as ledger state that Medusa
pulls and reconciles, never as a push. The `:4200` admin UI drives every action
against the scraper config API; Medusa only ever pulls and acks.

Everything is scoped by SKU / `source`, never by `company_id`. The scraper and its
vendors can share a Medusa account, so a removal must only ever touch products the
scraper owns (rows in Medusa's `scraper_sync_ref`). This is a hard invariant on both
sides.

## The two stages (scraper config API, driven by :4200)

### Stage 1: Take offline (reversible)

    POST /config/v1/delist  {"sources": ["<name>", ...]}

- Sets every one of the sources' products to `qty 0` with `reason = 'delisted'`.
- Disables the source in the config store, so a scrape can never silently re-stock a
  vendor that was taken offline.
- Reversible: re-enable the source and re-scrape, and the products restock and
  republish from the normal inventory delta.

### Stage 2: Remove permanently

    DELETE /config/v1/sources/<name>

- Guarded: refuses with 409 while the source still has live (qty > 0) products. Take
  it offline first, so nothing is left orphaned-live in the shop.
- Purges the (now qty-0) products from the ledger, records one tombstone per SKU on
  the `removed` lane, and deletes the source config row so it leaves the `:4200` list.

Both stages are guarded like reset/purge: 409 if a produce run is active or a pull
holds a lease, so a removal never races an in-flight sync.

## The lanes Medusa consumes

Two additions to the existing pull-apply-ack loop. No change to the
variations/products behavior.

### Inventory lane: the delist marker

`GET /sync/v1/inventory` serves stock deltas. The payload now carries `reason` only
when a delist set it:

    { "sku": "<sku>", "quantity": 0, "reason": "delisted" }   delist: stock 0 AND status -> draft (hidden)
    { "sku": "<sku>", "quantity": 0 }                         ordinary out-of-stock: stock 0, status UNCHANGED
    { "sku": "<sku>", "quantity": <N> }                       restock / re-add: stock N AND status -> published

Medusa flips status only on scraper-owned SKUs, driven purely by the flag. The
"no reason" path is a strict no-op on status, so today's out-of-stock behavior does
not change.

Scraper side: `delist_source` sets `reason='delisted'`; a normal scrape write
(`populate_inventory`) writes `reason=null`, so re-scraping a re-enabled vendor
clears the marker and republishes; an auto-dropped product of an active vendor
(`populate_discontinued`) leaves `reason` null (plain out-of-stock).

### Removed lane: the tombstone

    GET  /sync/v1/removed?limit=N        paged; default 500, max 2000
      -> { "type": "removed",
           "items": [ { "external_id": "<sku>",
                        "payload": { "sku": "<sku>", "source": "<vendor>",
                                     "reason": "vendor_removed" | "discontinued" } } ] }
    POST /sync/v1/ack  [ { "type": "removed", "external_id": "<sku>", "status": "done" | "blocked" } ]

- `done`: Medusa deleted the product (+ variant, prices, product_extension, category
  links, S3 images, stale cart lines) and cleared its `scraper_sync_ref`. The
  scraper retires the tombstone. The ack is the "it is gone" signal.
- `blocked`: an open reservation (a live quote or order) blocks the hard-delete.
  The scraper keeps the tombstone, re-serves it on the next pull, and dead-letters it
  to `dead` after 5 consecutive blocks (surfaces in `/sync/v1/failures` and
  `/sync/v1/status`).
- `reason` is audit only. `vendor_removed` = a stage-2 removal; `discontinued` = an
  ordinary dead-stock purge. Both mean "delete this external_id."

Medusa triggers its own `removed` pull (a "Pull removals" button in `:4200`, and/or
folded into the catalog pull). It never initiates a removal.

## Re-add

A removed vendor comes back by re-adding the source and re-scraping. Both re-add
paths are clean:

- After a delist-only (no purge): the ledger product + Medusa `scraper_sync_ref`
  still exist at qty 0. Re-scrape serves qty > 0, and Medusa restocks + republishes
  from the delta. No delete happened, so it just flows back.
- After a full removal (purge): the ledger row is gone and Medusa deleted the
  product. Re-scrape recreates it. SKUs are deterministic, so a re-created SKU could
  collide with a still-pending tombstone; the scraper clears any tombstone whose SKU
  is now a live product (in `populate`), so a live product and a pending tombstone can
  never coexist and Medusa never deletes a freshly re-created product.

## Isolation and safety invariants

- Removal is scoped by SKU / `source`, never `company_id`. A product with no
  `scraper_sync_ref` on Medusa's side is untouchable.
- The shared base layer (variations, attributes, combinations) is never deleted by a
  per-source removal. An orphaned variation (0 products) auto-hides on Medusa: the
  catalog tree is bounded by published products, so it drops out of the filters and
  storefront on its own. No signal needed. Combination-level pricing rules persist by
  design (they are keyed by the attribute tuple, shared across the combination);
  Medusa's own orphan-cleanup soft-deletes them when a combination goes invalid.
- Every destructive scraper op (delist, purge, remove, reset) refuses (409) while a
  pull holds a lease, so it never races an in-flight ack.

## Schema

Ledger schema v3 (SCHEMA_VERSION = 3):

- `removed` table: one tombstone row per purged SKU (external_id, source, reason,
  state in {pending, dead}, sync_attempts). Served while `pending`; a `done` ack
  deletes the row; repeated `blocked` acks dead-letter it to `dead`.
- `inventory.reason`: nullable. `delisted` (admin take-offline, Medusa hides) versus
  null (ordinary out-of-stock, stays listed).

The version bump is what makes these reach an existing ledger: the DAL skips the DDL
re-apply and migration when a ledger is already at-version, so without bumping to 3
the new table and column would never land on the live dev ledger. The re-apply is
idempotent (CREATE IF NOT EXISTS) and the migration ALTERs `inventory.reason` in
place; an old ledger migrates on the first reopen.

## Acceptance test

The manual per-source delete/clean run by hand is the acceptance test for this
design: if delist -> purge -> delete behaves cleanly by hand, the `:4200` buttons
just automate it. On the scraper side: `stone_pipeline/tests/test_vendor_removal.py`
covers delist (source-scoped, reason marker, reversible), the live-products guard,
source-scoped purge, the tombstone lane (record, serve, done-retire,
blocked-retry-then-dead-letter), the re-populate tombstone clear, `delete_source`,
and the v3 migration.
