# Frozen response contracts (scraper side) for the :4200 integration

Exact shapes for the endpoints the Blokport integration binds to (S-1..S-6), verified against the actual
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
- Scope (S-7): the purge deletes only bundles replaced by their lot (`reason: "replaced_by_lot"`). The scraper
  never purges a gone listing (sold out or hidden) or a delisted vendor's products: their records stay.

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
`state` `"abandoned"`. A `products` row can also carry state `photo_unpublished` (additive, ledger v16): a product a
change waits on (pending or dirty) held back because it links a standardised photo the photos side has not published,
`error` `photo not published: N files (<first key>)`. It is NOT a dead-letter: it is never served, it clears itself
when a produce finds the files published, and a requeue (or a reset) cannot revive it. The lanes come in that order
(variations, products dead-letters, products held by an unpublished photo, removed, inventory), each newest-first
and each capped at `limit` (default 200, via `?limit=`), so the list can hold up to five times `limit` items.

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
  "variation_reconcile": { "orphaned": <int> },   // orphaned: DEPRECATED (C13), to be removed; seed-inclusive, not actionable -- use variation.retiring
  "product_photos": { "unpublished": <int> } }    // additive (v16): the S-2 photo_unpublished items; not a dead-letter, so not in the badge sum below
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
`inventory` at the attempt cap. A `products` row in state `photo_unpublished` (S-2) is not a dead-letter, so abandoning
it answers `409`; it clears itself once its photos are published.

| Status | When | Body |
|---|---|---|
| `200` | abandoned now | `{ "type", "external_id", "abandoned": true, "was": "gap_held" \| "dead" }` |
| `200` | already abandoned (idempotent, nothing written) | `{ "type", "external_id", "abandoned": true, "already": true }` |
| `400` | `type` or `external_id` missing | `{ "error": "abandon body must be {type, external_id}" }` |
| `400` | `type` not one of the four | `{ "error": "unknown type '<t>'; expected one of [...]" }` |
| `404` | no such row | `{ "error": "unknown <type> '<external_id>'" }` |
| `409` | the row exists but is not a dead-letter (still live or serving); nothing written | `{ "type", "external_id", "abandoned": false, "error": "not a dead-letter (not <state>)" }` |

`abandoned` is a boolean in every body. A `4xx` is a refusal, never a `200`.

## S-6 -- the colour decision (config server :8724, the existing review API)

No new endpoint. The colour item is one more card in the existing review queue (kind `colour_unconfirmed`),
decided with the existing statement call. Built by `stages/curate/review.colour_cards`, stored by
`stages/decisions.write_confirm_file`, served by `config/decisions_store/queue.list_pending`. Every call takes
`Authorization: Bearer <SCRAPER_CONFIG_TOKEN>` (`401 {"error": "unauthorized"}` without it).

When: a stone variety the backbone sells in several colours, a vendor listing that gives NO colour, and no
operator colour for that (vendor, variety) yet. ONE item per (vendor, variety, type). A NEW listing of it is held
until decided. A listing already in Medusa stays listed with the colour it has, and is covered by the item.

### GET /config/v1/review/variants

No paging, no query filter: the whole queue in one list (variety cards, then origin cards, then decision gaps,
each in `ref` order). Decided cards sort to the end; undecided ones keep their order. The client filters on
`kind == "colour_unconfirmed"`. Response `200`:
```
{ "variants": [ <item>, ... ],
  "counts": { "total": <int>, "decided": <int>, "undecided": <int> } }   // over the WHOLE list, every kind
```
One colour item:
```
{ "ref": "patagonia|quartzite|varsha",       // <norm variety>|<norm type>|<norm vendor>: one per vendor
  "kind": "colour_unconfirmed",
  "variant": "Patagonia",                    // the variety it decides for
  "stone_type": "Quartzite",
  "src": "varsha",                           // the vendor it decides for
  "colours": ["Black", "White", "Beige", "Gold", "Pink", "Multicolor"],   // the variety's backbone colours,
                                             //   then "Multicolor" (unless the backbone already has it)
  "count": 3,                                // listings (products) it covers
  "skus": ["VAR-1056", "VAR-1217", "VAR-2230"],
  "listings": [ { "source": "varsha", "scraped": "Patagonia" },     // every scraped spelling behind it,
                { "source": "varsha", "scraped": "Patagonia Quartzite Polished" } ],   // as on other cards
  "spellings": ["Patagonia", "Patagonia Quartzite Polished"],
  "image": "https://...",                    // the standardised main view if published, else the scraped
                                             //   lead photo ("" if neither)
  "reason": "No vendor colour: varsha gives none and Patagonia comes in Black, White, Beige, Gold and Pink, so
             none is guessed. Pick this supplier's Patagonia colour; it applies to all 3 listing(s). ...",
             // or "Colour unconfirmed: <vendor> names several colours ('<its text>') and there is no
             //   multicolour value, ..." when the vendor named a list the colour vocabulary cannot hold
  "color": "",                               // never prefilled: the item never guesses
  "decided": false,
  "current_action": null,                    // "colour" once decided
  "current_color": null,                     // the colour decided, applied on the next produce
  "scraped": "Patagonia", "src_url": "...", "description": "...",   // the first listing's evidence
  "nearest_existing": "", "score": "", "model_prob": "", "sources": [] }
```
`decided` / `current_color` show a decision made since the last produce. The next produce applies it and drops
the item.

### PUT /config/v1/review/decide (the existing statement)

Request: the item's own listings, variety, type and the chosen colour:
```
{ "listings": <item.listings>, "name": <item.variant>, "type": <item.stone_type>, "color": "<one of item.colours>" }
```
It stores one durable statement per listing, in one transaction (all or none). On the next produce, EVERY listing
of that vendor's variety takes the colour, by add or update only: the ones on the item, and any spelling the vendor
lists later. If statements on two spellings disagree, the latest one wins. Picking a colour the backbone variety
lacks (e.g. "Multicolor") adds it to that variety's documented colours, so it is assignable.

Response `200`:
```
{ "source": "varsha", "scraped": "Patagonia", "name": "Patagonia", "stone_type": "Quartzite",
  "color": "Gold", "origin": null, "result": "bound",                   // the first listing's outcome
  "listings": [ { "source": "varsha", "scraped": "Patagonia", "result": "bound" }, ... ],
  "decided": 2 }                                                        // statements stored (= listings)
```
Refusals (`{"error": "..."}`; nothing is stored). In the order the handler (`server._put_review_decide`) checks them:
| code | when | extra keys |
|---|---|---|
| `400` | the body is not an object; `listings` is malformed; no listing given | |
| `400` | a field has the wrong JSON type: `name`, `type`, `color`, `origin`, `source` or `scraped` is not a string, or `widen` is not `true`/`false` (`_str` / `_flag` raise `_BadRequest`, which dispatch maps to 400; the error names the field). Raised where the field is read, so a mistyped `color` comes after the `name`/`type` checks below | |
| `400` | `name` or `type` is missing | |
| `503` | the field kinds are not published yet (retry) | |
| `400` | `name` has no ASCII form | |
| `400` | `name` is not its own clean form | `clean` |
| `400` | `type` is not a Medusa stone type | |
| `400` | `color` is not a Medusa colour attribute | |
| `400` | `origin` is not an ISO-3166 country (not sent by the colour screen) | |
| `409` | the variety is retired | `retired: true` |
| `400` | the VE2 gate: the variety is archived in Medusa | `archived: true` |
| `400` | the VE2 gate: `color` is archived or internal in Medusa and the variety does not already carry it | `restricted: true` |
| `400` | the store refuses a listing (`InvalidDecision`, e.g. a spelling or name with no usable key) | |

The VE2 gate (`server._ve2_decide_gate`) is dormant until Blokport publishes the attribute status columns: until
then no colour or variety is restricted or archived. So "any Medusa colour is accepted", including one outside
`item.colours`, holds only while those columns are absent.

There is no stale-item refusal. Deciding an item the last produce already dropped still stores the statement
(`200`), which then applies on the next produce.

**For the FE: offer only `item.colours`.** The server accepts any Medusa colour, not only the item's list (unless
the VE2 gate restricts it), and it has no stale-item refusal. Limiting the choice to `item.colours` is therefore the
screen's job.

### Undo: DELETE /config/v1/review/decide (the existing call)

`{ "listings": <item.listings> }` clears each listing's statement, all or none (`server._delete_review_decide`):
- `200 { "listings": [...], "cleared": [{"statements": <n>}, ...] }`.
- `400`: the body is not an object, `listings` is malformed or empty, or a spelling folds to no key
  (`check_clearable`).
- `409 { "error": ..., "blocked": [{source, scraped, products}] }`: a produce applied the statement and live
  products are bound by it. Restate with PUT and another colour instead.
- `409 { "error": "cannot verify which live products depend on ... no produce output ..." }` (no `blocked`): a
  produce applied the statement, but this host has no produce output to check against.

A statement decided after the last applying produce on record has reached no product, so it is never blocked.
With no applying produce on record, the produce output decides: products it shows bound block the undo.

## S-7 -- a listing gone at its supplier stays published as sold out (inventory lane; table 505, agreed with M4)

Owner's ruling 11-11. A listing its supplier no longer has (sold or removed) keeps its record, photos and textures
permanently and comes back under the same SKU if it reappears.

GET `/sync/v1/inventory` item payloads:
```
{ "sku": "<SKU>", "quantity": 0, "made_to_order": false, "reason": "sold_out", "gone_at": "<UTC ISO>" }
    // gone, was published: stays published, stock 0, not buyable, shown as sold out
{ "sku": "<SKU>", "quantity": 0, "made_to_order": false, "reason": "no_photo" | "delisted", "gone_at": "<UTC ISO>" }
    // gone, was hidden: keeps its reason and stays hidden (no new display case)
{ "sku": "<SKU>", "quantity": <N>, "made_to_order": false }
    // reappeared: a plain stock, no reason, no gone_at
```
- `gone_at` is the first trusted scrape the listing was missing from, unchanged while it stays gone (a listing already
  gone before ledger v18 gets the first v18 run's date). It is absent on a
  listing that is not gone. Blokport v1 accepts it and does not store it, so it rides the listing's next served stock:
  a hidden listing whose reason does not change is not served again for `gone_at` alone.
- Hide reasons: `no_photo`, `delisted`, `held` (#632, stock unknown). Any other reason at stock 0 is shown (sold out) once the
  backend lane change lands. Today a stock 0 with NO reason is hidden (measured by M4), and the scraper always sends a
  gone listing with its reason.
- Never deleted: the scraper never sends a tombstone for a gone listing. The purge (S-1) skips it, a vendor removal
  unpublishes every product as `delisted` instead of tombstoning, and a hard or factory reset and a seed variety drop
  spare it. A reset re-serves a spared gone listing once from zero (Medusa is wiped too on a factory reset). A
  delisted (vendor-removed or taken offline) listing is spared the same way. A variety
  a gone listing shows cannot be retired or unminted (409, naming the gone SKUs).
- Condition of done (505): the backend lanes never unpublish or hard-delete a `sold_out` listing. The inventory lane
  stops rejecting it when it is not on sale, and `held` joins the hide set. The scraper half reaches a pull only with or
  after that change.

## Notes
- `combinations` is NOT a sync lane (dormant): it never appears in `/failures` and its `status.combination`
  histogram is not part of the dead-letter total.
- These shapes are stable; a change here is a versioned contract change, not silent drift.
