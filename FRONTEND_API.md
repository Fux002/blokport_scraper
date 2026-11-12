# :4200 frontend API reference (the review routes are documented in REVIEW_DECISIONS.md; this file is the older overview)

Every endpoint the Blokport admin (:4200) can call, taken directly from the two dispatchers
(`stone_pipeline/config/control/server.py`, `stone_pipeline/ledger/server.py`). **Anything not in this document is
not served -- remove it from the frontend.** Grouped by the three pages: Scraper, Sync, Diagnostics.

## Two backends, two tokens

| Backend | Default | Base path | Auth header | Token |
|---|---|---|---|---|
| Config server | `:8724` | `/config/v1/...` | `Authorization: Bearer <token>` | `SCRAPER_CONFIG_TOKEN` |
| Sync ledger server | `:8723` | `/sync/v1/...` | `Authorization: Bearer <token>` | `SCRAPER_SYNC_TOKEN` |

Call both from the `:4200` **server-side** with the token; never ship a token to the browser. Put a real
base URL behind your proxy. Every request is bearer-gated; a bad/absent token returns `401`.

---

## Scraper page  (config server :8724)

### Sources
| Method | Path | Purpose | Body / response |
|---|---|---|---|
| `GET` | `/config/v1/sources` | list every scraper, enriched with `scrape_at`, `scrape_rows`, `ledger_products` | `{ "sources": [ <source>, ... ] }` |
| `GET` | `/config/v1/sources/<name>` | one source | `<source>` or `404` |
| `PUT` | `/config/v1/sources/<name>` | create OR update (full object; missing fields fall to defaults). **Also the promote-to-auto action: PUT with `"mode":"auto"`.** | body `<source>` -> saved `<source>`; `400` on a bad/duplicate/adapterless source |
| `DELETE` | `/config/v1/sources/<name>` | remove permanently (purge qty-0 products + drop config) | `409` if the source still has LIVE products (delist first) |

A `<source>`'s `lifecycle` is exactly one of `active` | `paused` | `delisted`.

#### Image pipeline switch (`image_pipeline`, 2026-11-02)
`<source>` carries **`image_pipeline`** (bool, default `true`): suggested label **"Image pipeline"**, a toggle on the
source edit page and a column/badge in the list.
- **on**: the photo process (STANDARDISE_PROCESS.md) works the source's listings; a product is listed only with a
  standardised photo (without one it is served at stock 0 with reason `no_photo` and kept for review). A logo,
  watermark or overlay text on a photo is found per photo by the judge and removed by the hosted clean step; there
  is no per-source watermark setting any more.
- **off**: the products show the scraped photos (re-hosted, size-reduced; no GPU, no hosted call), stay listed, and
  the photo process never picks them (nothing is spent on them). Handy for testing a source.

**Retired fields (remove from the admin):** `enhance` (the "Enhance (upscale)" toggle, its form default
`enhance=true`, the "Enhance on (billable)" badge, `enhance` in the source type), `watermarked` (the "remove
watermark" / "de-watermark" toggle) and `fal_prompt`. They are no longer returned; a PUT that still sends them is
saved without them (ignored with a server-side warning, as every unknown field is). Every existing source
starts with `image_pipeline` on (config.db migration, schema 9); turning it off is a deliberate admin choice.

### Run (the "produce" trigger)
| Method | Path | Purpose | Body / response |
|---|---|---|---|
| `POST` | `/config/v1/run` | trigger a produce | `{ "sources"?: [...], "stage"?: "scrape"\|"catalog"\|"republish"\|"inventory"\|"all"\|"photos" }` -> `202` + run record, or `409` in-flight. `photos` (no sources) is the standardised-photo process (STANDARDISE_PROCESS.md); with it switched on it also follows every successful `all` as its own run (never `republish` or `catalog`). A photos run's record carries its summary as `counts` (chosen, review, pending, spend, budget_reached) and is `succeeded` unless a real error stopped it. Any other stage asked for while a photos run holds the slot stops it and starts at once (`202`); that photos run ends with status `stopped` (its work is kept). `"close_only": true` (stage `photos` only, else `400`; a boolean, else `400`) is an operator action, not a UI button: after a close-up rule change, the photos run makes the chosen photos' close-ups again instead of picking and judging (STANDARDISE_PROCESS.md); every run record carries `close_only` (false unless asked) |
| `GET` | `/config/v1/run` | current + last run | `{ "current": <run>\|null, "last": <run>\|null }` |
| `GET` | `/config/v1/run/<run_id>` | one run by id | `<run>` or `404` |

### Lifecycle (all take `{ "sources": [...] }`)
| Method | Path | Purpose |
|---|---|---|
| `POST` | `/config/v1/pause` | freeze a source (stop scraping; products stay live) |
| `POST` | `/config/v1/resume` | unfreeze |
| `POST` | `/config/v1/delist` | take offline (stock 0 + disable) -- reversible |
| `POST` | `/config/v1/purge` | hard-delete the qty-0 (dead-stock) products; returns external_ids to delete in Medusa |
| `POST` | `/config/v1/clean` | prune superseded scrapes/runs; `{sources}` deletes those sources' raw scraped data |
| `POST` | `/config/v1/reset` | clean-start the ledger sync state; `{ "hard"?: bool, "sources"?: [...] }`; factory reset `{ "pristine": true, "keep_images"?: bool, "keep_scrape"?: bool }` |
| `POST` | `/config/v1/variations/<key>/retire` | remove a variety; `{ "force"?: bool }` |
| `POST` | `/config/v1/variations/<key>/un_retire` | reverse a retire |

### Dropdowns / vocab (all `GET`)
| Path | Returns |
|---|---|
| `/config/v1/adapters` | `{ "adapters": [<coded adapter name>, ...] }` (validate the source "adapter" field) |
| `/config/v1/colors` | `{ "colors": [...] }` (mint-colour picker) |
| `/config/v1/types` | `{ "types": [...] }` (mint-type picker) |
| `/config/v1/varieties` | `{ "varieties": [...] }` (alias-to dropdown) |

### Review queue (the new-variety / attribute / backbone decisions; applies NEXT produce)
| Method | Path | Purpose |
|---|---|---|
| `GET` | `/config/v1/review/variants` | pending varieties (+ current_action) |
| `PUT` | `/config/v1/review/variants/<variant>` | `{ "action":"reject" }` only; everything else is a statement: `PUT /config/v1/review/decide` (see REVIEW_DECISIONS.md; also `GET /review/resolved`, `POST /purge`, `POST /curation/rebuild`, `POST /variations/<key>/not_a_duplicate`) |
| `GET` | `/config/v1/review/attributes` | pending attribute values (need a Medusa id) |
| `PUT` | `/config/v1/review/attributes/<value>` | `{ "kind":..., "medusa_id":... }` |
| `GET` | `/config/v1/review/backbone` | pending leaf additions |
| `GET` | `/config/v1/review/backbone/decided` | already-decided leaves (to revise) |
| `POST` | `/config/v1/review/backbone/approve_all` | `{ "verdict"?: "likely_real" }` bulk-approve |
| `PUT` | `/config/v1/review/backbone/<ref>` | `{ "action":"approve"\|"reject"\|"clear" }` |
| `GET` | `/config/v1/review/aliases[?status=proposed\|active\|rejected]` | learned aliases (P2-10 C6): `{aliases, counts, open, open_read}`; a card is `{id, identity_key, variety, stone_type, value, norm, method, source, score, status, first_seen, decided_by, decided_at}` (`variety`/`stone_type` null when the ledger no longer holds the Key, `score` null when unknown); `open` is the cleanup's read-only items `{kind, key, value, reason}`, `open_read` `current`\|`absent`; 503 names an absent ledger or an unreadable open file |
| `PUT` | `/config/v1/review/aliases/<id>` | `{ "action":"approve"\|"reject" }`; `<id>` = url-encoded `identity_key\|norm`; reject also withdraws an approved alias, and a rejected norm is never proposed again |
| `DELETE` | `/config/v1/review/aliases/<id>` | the only removal of a learned value (it may be proposed again); no reset clears learned aliases, an unmint or retire drops the variety's |

Every `GET /config/v1/review/variants` card carries `resolution: {action: "bind"|"mint", name, stone_type}` (null with no name or type): the server's bind-or-mint answer for the card's prefilled name and type (the standing decision's, else the scraper's guess), from the same rule a statement binds by. It replaces the client-side "Will mint" guess.

> **Reset option "Keep hosted images" (`keep_images`, factory reset only).** On: the hosted product images stay
> (everything under `<env>/products/`: the scraped originals, the re-hosted photos, the published standardised views
> and 3D slabs), so the products link their standardised photos at once. Off: all of them are wiped and the next
> scrape re-hosts the photos; the photo process's stored results are never wiped, so its next run publishes the
> chosen standardised photos again without paying. Suggested label: "Keep hosted images" (was "Keep enhanced
> images").

---

## Sync page  (sync ledger server :8723)

| Method | Path | Purpose | Body / response |
|---|---|---|---|
| `GET` | `/sync/v1/status` | ledger-wide entity counts + sync progress | `<status>` |
| `GET` | `/sync/v1/failures` | drill-down behind the status `gap_held` count (what Medusa rejected + why) | `{ "failures": [...] }` |
| `GET` | `/sync/v1/variations?status=ready&limit=N` | the ready variations delta (N default 500, max 2000) | `{ "type":"variations", "items":[...] }` |
| `GET` | `/sync/v1/products?status=ready&limit=N` | ready products delta | `{ "type":"products", "items":[...] }` |
| `GET` | `/sync/v1/inventory?status=ready&limit=N` | ready inventory delta | `{ "type":"inventory", "items":[...] }` |
| `GET` | `/sync/v1/removed?status=ready&limit=N` | ready removals (tombstones) | `{ "type":"removed", "items":[...] }` |
| `POST` | `/sync/v1/ack` | ack applied entities | body `[{type, external_id, medusa_id, status}, ...]` -> `{ "acked", "missed", "skipped" }` |
| `POST` | `/sync/v1/requeue` | un-quarantine dead-lettered (`gap_held`) entities back to dirty | `{ "type"?: "variations"\|"products" }` (omit for both) -> `{ "requeued": N }` |

> `status=ready` is the only served status. `removed` acks use `status` `done` (retire) or `blocked`
> (keep + retry). These are Medusa's pull-job endpoints; the Sync page is a read view of `/status` +
> `/failures` plus the `requeue` recovery button.

> **Clearing a `removed` dead-letter (state `dead`).** `requeue` does NOT cover the removed lane (only
> variations/products), and a plain "Pull removals" will NOT re-serve a `dead` tombstone -- the serve query
> is `state='pending'`, and `dead` (hit after repeated `blocked` acks, usually because the variety still had
> live children) does not serve. The recovery is **re-retire**, NOT a requeue: `POST
> /config/v1/variations/<key>/retire` re-records the tombstone, which resets it `dead -> pending` (and
> re-cascades any remaining products). That endpoint 409s if the variety still has LIVE products, so the
> operator path is: **delist/move the blocking live products (or pass `force:true`) -> re-retire the variety
> -> the next Pull removals now serves it.** No removed-requeue endpoint is needed -- re-retire is the
> complete, intent-re-asserting mechanism.

---

## Diagnostics page  (config server :8724)  -- NEW

| Method | Path | Purpose | Response |
|---|---|---|---|
| `GET` | `/config/v1/diagnostics` | every source's latest per-layer diagnostic | `{ "diagnostics": [ <diag>, ... ] }` |
| `GET` | `/config/v1/diagnostics/<source>` | one source's latest diagnostic | `<diag>` or `404` (never produced) |

`<diag>`:
```
{
  "source": "polonine", "run_id": "polonine_20260625_222933",
  "health": "OK"|"DEGRADED"|"FAILED", "magnitude": "OK"|"DEGRADED"|"FAILED",
  "gates": { "ingest":"OK", "clean":"OK", "process":"OK" },
  "stages": [ { "stage":"normalize", "status":"OK"|"DEGRADED"|"FAILED"|"skipped"|"not_run",
                "rows_in":303, "rows_out":303, "rejected":0, "reviewed":0, "gapped":0, "extra":{...} }, ... ],
  "drift": [ { "kind":"fill_drop"|"new_column"|"missing_column"|..., "field":"color",
               "detail":"...", "likely_rename":"..." }, ... ],
  "row_count":303, "row_baseline":303, "updated_at":"<iso>",
  "admission": { "state":"review"|"eligible"|"auto"|"demoted", "streak":3, "required":3,
                 "certified":true, "eligible":true, "mode":"review", "last_drift":["fill_drop",...] },
  "images": { "image_pipeline":true, "total":412, "ready":388, "hidden":24, "generating":false,
              "held_for_image":0 }
}
```

`images` (counted from the sync ledger when the diagnostic is served, over the source's **listable** products: in stock,
or hidden for want of a standardised photo; a sold-out or delisted product is not counted. Absent when the source has
no listable product in the ledger: render "-"). `total = ready + hidden + the rest`:
- `image_pipeline`: the source's switch.
- `total`: the source's listable products.
- `ready`: listed (not hidden) with a published standardised photo as their thumbnail; for a source with
  `image_pipeline` off, listed with a hosted photo (they show the scraped ones).
- `hidden`: served at stock 0 with reason `no_photo` (no standardised photo yet; always 0 when the switch is off).
- the rest (`total - ready - hidden`): listed without a standardised photo, which happens while the no_photo rule
  cannot tell (a photo pool that could not be read) or where the rule is off (dev before #482), or, with the switch
  off, listed without any photo.
- `generating`: a photos run is under way and works this source.
- `held_for_image`: products the last produce could not emit for want of a photo that a republish can still emit
  (from the last produce's run summary, counted apart from the ledger counts above).
- Retired: `held` (the old enhancer's discard markers). `ready` no longer means "de-watermarked + enhanced".

Render per source:
- a **layer strip**: `health` + `gates` + each `stages[].status`, colour-coded (green OK / amber DEGRADED /
  red FAILED). A bug shows at its layer.
- a **"source format changed" banner** when `drift` is non-empty (show `kind` + `field`).
- the **admission badge**: `eligible` -> a "Promote to auto?" prompt (action = `PUT /config/v1/sources/<name>`
  with `mode:auto`); `demoted` -> an alert ("auto-demoted: drifted"); `review`/`auto` -> normal badge.

---

## Cleanup checklist for the frontend
- Any call to a `/config/v1/...` or `/sync/v1/...` path NOT listed above is stale -- delete it.
- There is **no** dedicated "promote"/"demote" endpoint: promotion is `PUT /config/v1/sources/<name>` with
  `mode:auto`; demotion is automatic (the admission rule) and surfaces via the diagnostics `admission.state`.
- There is **no** endpoint that returns pixels.
- Ports are `8724` (config) and `8723` (sync); do not hardcode `:4200` as a backend -- that is the frontend
  host that proxies to these two.

---

## Backend hardening (audit fixes) -- what :4200 must handle

Response shapes are UNCHANGED. These backend fixes only make existing fields fire more often; the UI already
renders all of them, so there is **nothing to change** -- just be aware:
- **`stages[].validate` and `stages[].images` may now be `"DEGRADED"`.** They used to be always `"OK"`. Now
  `validate` goes DEGRADED when a majority of rows are rejected, and `images` when a large fraction have no
  publishable image. The layer strip already colour-codes DEGRADED (amber), so no code change -- but a green
  strip is now more trustworthy (previously it hid a mass-reject / mass-no-image run).
- **`admission.state` may be `"demoted"` more often.** Auto-demote now also fires on the produce/ECS path and
  on a catastrophic empty scrape (it previously only ran on the local path). The demoted alert already exists.
- **Diagnostics now persist for aborted runs too**, so a failed run is visible in `GET /config/v1/diagnostics`
  with `health:"FAILED"` -- same shape, just more complete history.

**Variant-name casing (no action -- self-resolves):** the ALL-CAPS `ICE BURG` seen in the review table was
STALE data from the pre-refactor build. The frontend renders the name verbatim (no `text-transform`), and the
backend now title-cases at source (`title_case` in curate), so the next produce on the new build writes
`Ice Burg`; existing product titles refresh on the following pull (upsert by stable SKU). No duplicates, no
reset needed.
