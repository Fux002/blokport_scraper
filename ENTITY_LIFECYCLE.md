# Entity lifecycle: sources and variations

The one model for adding and removing both kinds of entity the scraper owns: **sources** (scrapers) and
**variations** (varieties). It supersedes the source-lifecycle notes and the removal half of
VENDOR_REMOVAL.md, and is the contract both the scraper and Medusa build against. Companion to
SYNC_LEDGER_DESIGN.md. No em dashes (design principle 2).

## Principle

Blokport PULLS, the scraper SERVES, Blokport ACKS. Removal for BOTH entity kinds flows through ONE
generalized `removed` tombstone lane (a `kind` field). Every removal is an EXPLICIT operator action;
nothing auto-deletes (a variety a source merely stopped scraping is never removed). Everything is scoped
by SKU / source_code / variation Key, never by `company_id`.

## States

- **Source** `lifecycle` (config store): `active | paused | delisted | removed`.
- **Variation** ledger `state`: `pending -> dirty -> syncing -> synced` (convergent), `gap_held`
  (dead-letter), and `retiring` (deliberately removed, held until Medusa confirms the delete).

## Operations (the one guarded lifecycle module, stone_pipeline/lifecycle.py)

Source verbs (config API, driven by :4200):

    POST   /config/v1/pause    {"sources":[...]}     freeze: stop scraping, products stay live & buyable
    POST   /config/v1/resume   {"sources":[...]}     un-freeze; returns restock_required for a delisted source
    POST   /config/v1/delist   {"sources":[...]}     qty 0 + reason=delisted + lifecycle=delisted (offline)
    POST   /config/v1/purge    {"sources":[...]?}    hard-delete qty-0 products (product tombstones)
    POST   /config/v1/reset    {"hard":?, "sources":[...]?}   re-serve overlay (soft) / drop products (hard)
    DELETE /config/v1/sources/<name>                 remove: 409 if live products; purge + delete config
    POST   /config/v1/clean    {"sources":[...]?}    prune raw scrape files (409 on a paused/delisted source)

Variation verbs:

    POST   /config/v1/variations/<key>/retire     {"force":?}   remove a variety (404 unknown; 409 live products unless force)
    POST   /config/v1/variations/<key>/un_retire                reverse a retire (mirrors source resume)

All destructive ops share ONE exclusion guard (mutually exclusive with a produce run and each other) and
ONE resolve/translate. `retire`/`un_retire` also update the durable exclusion memory (retired_keys.csv),
so a produce never re-mints a retired variety, and un_retire lets it come back.

## The one removed lane

    GET /sync/v1/removed
      { "type":"removed", "items":[
         { "external_id":"<sku>", "payload":{ "kind":"product",   "sku":"<sku>", "source":"<source_code>", "reason":"vendor_removed"|"discontinued" } },
         { "external_id":"<key>", "payload":{ "kind":"variation", "key":"<key>", "reason":"variation_removed"|"rekey" } }
      ] }
    POST /sync/v1/ack  [ { "type":"removed", "external_id":"...", "status":"done"|"blocked" } ]

- Products serve BEFORE variations (a variant with children cannot be deleted); Medusa deletes children first.
- `kind=product` -> delete product + scraper_sync_ref + media + cart lines.
- `kind=variation` -> delete the variation ENTITY + its variants/prices/media/category-links (NEW consumer).
- Ack `done` retires the tombstone (and, for a variation, hard-deletes the childless ledger row); `blocked`
  retries, dead-letters after 5.
- A never-synced variety emits NO tombstone (Medusa never received it) and is dropped locally.

## Variant images (scraper-owned, end to end)

The variety texture `{Key}.png` is the scraper's alone: FAL-generated on demand during produce, at S3
`<env>/variations/{Key}.png`, served as the variations lane's `image_url`. Blokport only copies it. A
variation may be transiently imageless (`image_url` null) before generation (normal, not a bug); a minted
variety is held from the lane until its `{Key}.png` exists, so anything Blokport receives carries its
texture. No manual override.

## Re-key (type/name correction)

A correction that changes the canonical name/type mints a NEW Key. Sequence: the new variety appears and
syncs -> its products re-point to it on the scrape -> the operator retires the old Key -> the old variety
arrives on `removed(kind=variation)` and Medusa deletes it. Net: one new variant, one deletion.

## Reconcile (GET /sync/v1/status)

Per-entity state histograms + `inventory:{total, delta}` (reconcile inventory as `total - delta`).
`variation.retiring` surfaces retiring varieties; `variation_reconcile.orphaned` counts synced, rendered
varieties with 0 products (a source-removal orphan or a re-key old side awaiting retire) so the operator
can drive drift to zero. Counts are raw histograms, NOT servable counts.

## Invariants

- Provenance-scoped by external_id / source_code, never company_id.
- SKU = `<SOURCE_CODE>-<rest>`; `split_part(external_id,'-',1)` is a safe per-source scope.
- Base variations are never auto-deleted; removal (source or variation) is always explicit.
- Combinations are not served or retired by the scraper: Medusa rebuilds priceable tuples and orphan-cleans them.
- The lifecycle state in config.db + the retire memory in state/ must be snapshotted to survive an ECS
  restart (durability lane); until then, treat lifecycle as non-durable across a scraper redeploy.
