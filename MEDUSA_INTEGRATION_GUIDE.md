# Medusa integration guide: how to sync with the stone pipeline

Audience: the Medusa backend team. This is the contract for how Medusa connects to
the pipeline, what to pull, what to send back, and why the two systems stay in sync.

No em dashes anywhere in this document (repo convention).

> Revision: folds in the backend review. Key changes: payloads carry ZERO Medusa ids
> (everything is an external reference Medusa resolves); images are ingestion sources,
> not runtime hot-links; the name-resolution and attribute behaviour is specified;
> combinations ownership is called out as a decision, not deferred; `bundle_size` is
> flagged against the in-flight pallet model; routes are versioned (`/sync/v1`).

---

## 0. The model in one paragraph

The pipeline keeps a per-environment **ledger** of the desired catalog state, addressed
by stable **external ids** (a `Key` for each variety, a `SKU` for each product). Medusa
**pulls** what is ready from a small read-only HTTP API, applies it, and **acks** back
the id it minted. The pipeline never pushes into Medusa, and it holds **no Medusa ids**:
every reference in a payload (vendor, colour, finish, type, country) is an external name
or key that Medusa resolves on its side. Once an entity is acked it stops being served,
so re-pulling only ever moves the un-synced delta. The sync is **pull-driven, ordered,
idempotent, and convergent**.

---

## 1. How to connect

- **Per environment.** Dev and prod are fully separate (ledger, ids, base URL, token).
- **Transport.** Inbound HTTPS to the scraper sync service. JSON in, JSON out.
- **Auth.** Bearer token per env: `Authorization: Bearer <token>` (`BLOKPORT_SYNC_TOKEN`,
  from SSM). Missing or wrong -> `401`.
- **Versioned routes.** Everything is under `/sync/v1/` so the two systems can evolve
  behind a boundary:
  - `GET /sync/v1/status`
  - `GET /sync/v1/<type>?status=ready` where `<type>` is `variations`, `products`, `inventory`
  - `POST /sync/v1/ack`

---

## 2. Two independent flows (run them separately)

| Flow | Endpoints | What it does | Cadence |
|---|---|---|---|
| **A. Catalog sync** | `variations`, `products` | Creates and updates the products customers see | On change (after a scrape, or daily) |
| **B. Inventory sync** | `inventory` | Updates the stock quantity of products **already listed** in Medusa | Frequent (e.g. hourly), independent |

**Inventory sync never creates a product.** Enforced server-side: the `inventory`
endpoint only serves stock for products **already synced** (already in Medusa). A
not-yet-created product does not appear in the inventory feed, so an inventory run
cannot add a listing. Adding and updating listings is only ever the catalog flow.

---

## 3. No Medusa ids in payloads: the resolution contract

Every reference the scraper sends is an **external name or key**, resolved by Medusa to
its own id. The scraper holds no `company_id`, `sales_channel_id`, or port ids. This is
symmetric across all fields:

| payload field | what the scraper sends | Medusa resolves to |
|---|---|---|
| `category`, `type` (on the **variation**) | canonical name | the category / type id |
| `color`, `finish`, `quality` (on the **product**) | canonical name | the attribute id |
| `vendor` (on the product) | the source key (e.g. `polonine`) | the marketplace company + sales channel |
| `origin_port` | a UN/LOCODE (e.g. `ITMDC`) | the specific port id (lane precision) |
| `origin_country_code` | ISO2 (e.g. `IT`) | fallback only, when no port is resolved |
| `variation_external_id` | the variety `Key` | the variation id (stored as `external_id`) |

**Ports: send the origin port, not just the country.** Freight is priced on a specific
origin lane (Carrara is not Naples), so country alone is too lossy. The precise,
still-decoupled answer is `origin_port` as a **UN/LOCODE** that Medusa resolves to its
port id. `origin_country_code` is a fallback for when a specific port cannot be resolved
(and Medusa should treat that as approximate). One open item for the pipeline side: the
origin resolution is country-level today, so populating `origin_port` to LOCODE precision
is a reference-data enrichment (origin city/lane -> LOCODE), and we should confirm whether
the origin port is product- or vendor-specific. Until then the pipeline sends
`origin_country_code` and Medusa applies its default, flagged as approximate.

**Unknown-name handling differs by field, and must be written down:**
- **Attributes** (`color`/`finish`/`quality`/`category`/`type`): on an unresolved name,
  Medusa may **auto-create** the value (the pipeline only ever sends names it validated
  as canonical, so this is safe) or **ack `failed`** and the pipeline holds the entity.
  Either is fine; pick one.
- **`vendor`: strict, never auto-create.** An unseen source key resolving to a NEW
  marketplace company + sales channel is a business-onboarding event, not a silent
  create. On an unknown `vendor`, Medusa **acks `failed`** and surfaces it; the pipeline
  holds those products until the vendor is onboarded. Do not auto-create a company.

**Attributes are not a push flow.** Medusa owns the attribute vocabulary. The scraper
does not push attributes; it sends canonical names and Medusa resolves them (above). The
`attribute` count in `/sync/v1/status` is just the pipeline's read-only mirror of that
vocabulary (seeded from a one-time export, used for validation), not a feed you consume.
When the pipeline discovers a brand-new value, it surfaces it for review and holds the
dependent entity until the value exists; it never silently lists with a null attribute.

---

## 4. Flow A: catalog sync (variations, then products)

The server enforces order: a product is only served once its variation is synced and its
variety texture is live. Pull in two steps and loop until both are empty.

### Step 1: variations

```
GET /sync/v1/variations?status=ready&limit=500
```

```json
{
  "type": "variations",
  "items": [{
    "external_id": "block_marble_breccia_oniciata_5ca3e544-...",
    "payload_hash": "9f3a...",
    "payload": {
      "category": "Blocks",
      "type": "Marble",
      "name": "Breccia Oniciata",
      "aliases": ["Breccia Oniciata Marble", "Marmo Breccia Oniciata"],
      "colors": ["White", "Beige", "Grey"],
      "finishes": ["Polished", "Honed", "Brushed"],
      "qualities": ["First", "Commercial"],
      "image_url": "https://.../variations/block_marble_breccia_oniciata_5ca3...png",
      "volume": 0.0014348
    }
  }]
}
```

`volume` is a **number** (m3 per kg), not a string. Dimensions and `weight` on products
are numbers too. Names stay strings; ids never appear.

**A variation always belongs to a category (strict).** `category` and `type` are
intrinsic to the variety: its identity is the (category, type, name) triple. They live on
the variation, and a product **inherits both** from the variation it points at, so a
product never sends `category` or `type`. Apply: upsert by `external_id` (store the
`Key`); resolve the `category` and `type` names to your ids; ack with the variation id
Medusa minted. `image_url` is an **ingestion source** (section 6), not a runtime link.

`colors`/`finishes`/`qualities` are the variety's **allowed sets** (from the backbone
tree): the colours, finishes, and qualities this variety can be priced in. They travel
in the variation payload on purpose, so building `valid_combination` needs no out-of-band
side-channel (section 10).

### Step 2: products

```
GET /sync/v1/products?status=ready&limit=500
```

Only products whose variation is synced and whose texture is live.

```json
{
  "external_id": "POLONINE-7F3A2B19",
  "payload_hash": "1c80...",
  "payload": {
    "variation_external_id": "slab_travertine_walnut_8a1c...",
    "color": "Brown", "finish": "Honed", "quality": "First",
    "vendor": "polonine",
    "title": "Walnut Travertine Honed Slab",
    "description": "Walnut Travertine is a brown travertine ...",
    "handle": "walnut-travertine-honed-slab-polonine-7f3a2b19",
    "length": 2.5, "width": 0.2, "height": 2.0, "weight": 0.3,
    "origin_port": "ITMDC", "origin_country_code": "IT",
    "bundle_size": 7,
    "image_urls": ["https://.../products/improved/polonine/<sha>.jpg"]
  }
}
```

Apply: upsert by `external_id` (the `SKU`); resolve `variation_external_id` to the
variation id from step 1 and **inherit its category and type**; resolve the `color`/
`finish`/`quality` names and `vendor`; resolve `origin_port` (UN/LOCODE) to the port id
(falling back to `origin_country_code`); copy `image_urls` into your own storage. Ack
with the product id.

> Two fields are shipped pending coordination with the pallet-model work, so a sync
> update never silently overrides it:
> - **`bundle_size`** is the bundle multiplier the pallet model is retiring (selling unit
>   decoupled from logistics unit). Agree whether to drop it and rely on pieces +
>   dimensions.
> - **`weight`** is sent alongside `length/width/height`, but the pallet model computes
>   weight from dimensions x density. Two sources for one number. Decide which wins; the
>   recommendation is **dimensions are truth, Medusa recomputes weight** and ignores the
>   sent `weight`, so the sync cannot override the pallet model's value.

Loop both until empty -> the catalog is in sync.

---

## 5. Flow B: inventory sync (stock only, existing products only)

```
GET /sync/v1/inventory?status=ready&limit=1000
```

```json
{ "type": "inventory", "items": [
  { "external_id": "POLONINE-7F3A2B19", "payload": { "sku": "POLONINE-7F3A2B19", "quantity": 7 } },
  { "external_id": "VARSHA-1C2D3E4F",   "payload": { "sku": "VARSHA-1C2D3E4F",   "quantity": 0 } }
]}
```

Apply: find the product by `SKU` and **set its STOCKED quantity** (not its available
quantity). Never create. **Respect reservations**: a `0` delist on a product that has
open-order reservations must drop available to 0 without breaking the reservation backing
a live order, so set stocked-not-available and do not fight the reservation invariant.

`quantity: 0` is the explicit **discontinued** signal, not a transient stockout: in this
feed an in-stock product never reports 0 (the pipeline floors a real stock to 1), so a 0
always means the supplier **dropped** the product. Medusa can act on the 0 directly (no
time-window guessing). It is reversible: a later scrape that carries the stone again sends
a positive quantity and un-retires it. Ack each (no `medusa_id`).

---

## 6. Images are ingestion sources, never hot-links

`image_url` (variations) and `image_urls` (products) point at the pipeline's storage.
They are **ingestion sources**: on apply, Medusa **copies them into its own S3** (and
runs its own watermark/thumbnail pipeline). Do **not** reference these URLs at runtime,
or the storefront becomes permanently dependent on the pipeline's image host. The
"texture must be live" gate exists only so the source image is present at ingestion time;
after copy, Medusa owns the image.

---

## 7. What to send back: the ack

```
POST /sync/v1/ack
[
  { "type": "variations", "external_id": "block_marble_...", "medusa_id": "variation_01ABC...", "status": "created" },
  { "type": "products",   "external_id": "POLONINE-7F3A2B19", "medusa_id": "prod_01XYZ...",     "status": "updated" },
  { "type": "inventory",  "external_id": "VARSHA-1C2D3E4F",   "status": "updated" }
]
```

Response: `{ "acked": 3 }`.

- `medusa_id` is required for `variations` and `products`; omit for `inventory`.
- `status`: `created`/`updated`/`skipped` (success) or `failed` (returns the entity to
  the queue for the next pull).

**Ack AFTER you commit.** Persist the `external_id -> entity` mapping in Medusa before
you ack. If you ack and then crash before committing, the ledger marks it synced while
Medusa lost it, so it would not be served again until its content changes. Upsert-by-
external_id makes a re-pull safe, but only if the ack follows the commit.

**Acks are idempotent.** Re-acking the same `external_id` is a no-op, so a failed ack
POST can be retried safely.

---

## 8. The sync guarantee (why this stays in sync)

- **Pull-only.** The pipeline never writes into Medusa; you pull when ready.
- **Ordered.** Served only when prerequisites are synced: product after variation +
  live texture; stock after product. You cannot load out of order.
- **Convergent.** An acked entity stops being served; re-pulling moves only the delta.
- **Idempotent.** Every apply is an upsert by `external_id`; `payload_hash` lets you skip
  an unchanged item.
- **Observable.** `GET /sync/v1/status` is the monitoring surface, per type and state:
  - `pending` / `dirty` = work waiting to be pulled.
  - `gap_held` = entities the pipeline is deliberately holding back (a variety with no
    resolvable type, a product whose texture is not live, a referenced value not created
    yet). These need an operator action on the **pipeline** side, not a Medusa fix.
  - `synced` = applied and acked.
  - inventory `delta` = stock that moved since the last sync.

  `pending`/`dirty`/`delta` all at zero means the two systems are in sync. Poll it to
  confirm convergence, and alarm on a rising `gap_held` or a stuck `pending` (the signal
  that a pull job stalled or a hold needs attention). The `ack` response returns
  `{acked: N}`; per-item ack results can be added if you want finer failure visibility.

---

## 9. Ownership, safety, retirement

- **Pipeline-owned products are read-only in Medusa.** Only entities the pipeline owns
  are served (their `external_id` is our `Key`/`SKU`). Products that website users listed
  are never in the feeds, so neither flow can touch them. Policy: Medusa **badges
  pipeline-owned products read-only**, so a marketplace user cannot edit or override one
  out from under the sync. Stating the policy is the pipeline's; enforcing the badge is
  Medusa's build-time work.
- **Retirement signal = quantity 0.** A discontinued product is delisted by setting stock
  0, which in this feed is the unambiguous "supplier dropped it" signal (an in-stock
  product never reports 0; it floors to 1). So Medusa archives on the 0 directly, not on a
  time-window guess. Reversible: a later scrape un-retires it with a positive quantity.
  The pipeline never hard-deletes; archiving the stock-0 set is Medusa's build-time action.
- **Product attributes are always within the variety's allowed sets.** A product's
  `color`/`finish`/`quality` is guaranteed by the pipeline (its tree-reconcile stage) to
  be a member of the variation's `colors`/`finishes`/`qualities` (section 4). Medusa can
  rely on this, and may cross-check a product against the allowed sets it already received
  on the variation.
- Uncertain entities are held, not served (no resolvable type, texture not live), so
  nothing lists broken.

---

## 10. Combinations: Blokport owns the table, fed by the variation allowed sets

Valid combinations are the priceable `color x finish x quality` tuples per variety
(~2 million rows). The review is right that this needs deciding up front, because
**Blokport already builds `valid_combination` itself** (the tree to relational
migration). Two builders of the same 2M-row table is a real conflict.

**Decided (both sides agreed):** Blokport owns `valid_combination` generation. It has the
JSONB-tree to `valid_combination` machinery, combinations are relational backend data, and
2M rows do not belong in a pull feed. The pipeline does **not** send combinations through
the sync (it still builds `2_valid_combinations.csv` for its own legacy CSV path, outside
this integration).

**Source of truth for the allowed sets: the pipeline's backbone.** The per-variety allowed
sets (`colors`/`finishes`/`qualities`) travel in the variation payload (section 4), so
Blokport builds each variety's combinations directly from the variation feed, with no
out-of-band side-channel and no second tree to reconcile. Blokport's tree machinery
**consumes** our allowed sets rather than holding a competing definition. (A paged
`/sync/v1/combinations` pull stays a fallback only if the pipeline ever has to own the 2M
rows; not the path.)

---

## 11. Endpoint reference

| Method | Path | Purpose | Body / response |
|---|---|---|---|
| `GET` | `/sync/v1/status` | per-type / per-state counts | `{type: {state: count}, ...}` |
| `GET` | `/sync/v1/variations?status=ready&limit=N` | varieties to create/update | `{type, items:[{external_id, payload_hash, payload}]}` |
| `GET` | `/sync/v1/products?status=ready&limit=N` | products to create/update (variation synced, texture live) | same shape |
| `GET` | `/sync/v1/inventory?status=ready&limit=N` | stock deltas for already-synced products only | `{type, items:[{external_id, payload:{sku, quantity}}]}` |
| `POST` | `/sync/v1/ack` | record minted ids, mark synced | `[{type, external_id, medusa_id?, status}]` -> `{acked: N}` |

All requests require `Authorization: Bearer <token>`. `limit` is optional.

---

## 12. The two jobs, end to end

**Catalog job** (on change / daily):

```
loop:
  apply GET /sync/v1/variations?status=ready, then POST /sync/v1/ack
  apply GET /sync/v1/products?status=ready,   then POST /sync/v1/ack
until both are empty
```

**Inventory job** (frequent, independent):

```
apply GET /sync/v1/inventory?status=ready (set stock by SKU, never create), then ack
```

---

## 13. The one thing for the Medusa side (symmetry)

Independence cuts both ways. Implement the Medusa side as **one isolated sync-adapter
module** (the swappable-provider pattern): a single place that knows this HTTP contract
and translates it to Blokport entities (product / variation / inventory), and nothing
else in the backend imports it. If the pipeline ever changes or is swapped, you rewrite
that one module. Do not sprinkle `/sync` knowledge across the product or inventory
services.
