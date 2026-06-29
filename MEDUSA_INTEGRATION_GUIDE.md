# Medusa integration guide: how to sync with the stone pipeline

Audience: the Medusa backend team. This is the contract for how Medusa connects to
the pipeline, what to pull, what to send back, and why the two systems stay in sync.

No em dashes anywhere in this document (repo convention).

---

## 0. The model in one paragraph

The pipeline keeps a per-environment **ledger** of the desired catalog state, addressed
by stable **external ids** (a `Key` for each variety, a `SKU` for each product). Medusa
**pulls** what is ready from a small read-only HTTP API, applies it (creates or updates
the entity), and **acks** back the id it minted. The pipeline never pushes into Medusa
and never holds a transaction open against it. Once an entity is acked it stops being
served, so re-pulling only ever moves the un-synced delta. That is the guarantee: the
sync is **pull-driven, ordered, idempotent, and convergent**. Run it to empty and the
two systems are in sync.

---

## 1. How to connect

- **Per environment.** Dev and prod are fully separate (separate ledger, separate ids,
  separate base URL and token). Never mix them.
- **Transport.** Inbound HTTPS to the scraper sync service. JSON in, JSON out
  (`Content-Type: application/json`).
- **Auth.** A bearer token per env: `Authorization: Bearer <token>`. The token is the
  `BLOKPORT_SYNC_TOKEN` secret (held in SSM per env). A request without it gets `401`.
- **Endpoints** (full reference in section 9):
  - `GET /sync/status`
  - `GET /sync/<type>?status=ready` where `<type>` is `variations`, `products`, or `inventory`
  - `POST /sync/ack`

---

## 2. Two independent flows (run them separately)

There are **two jobs**, on **two schedules**, that do not interfere:

| Flow | Endpoints | What it does | Cadence |
|---|---|---|---|
| **A. Catalog sync** | `variations`, `products` | Creates and updates the products customers see | On change (after a scrape, or daily) |
| **B. Inventory sync** | `inventory` | Updates the stock quantity of products **already listed** in Medusa | Frequent (e.g. hourly), independent |

**The inventory flow never creates a product.** This is enforced server-side: the
`inventory` endpoint only ever serves stock for products that are **already synced**
(already in Medusa). A product that has not been created yet does not appear in the
inventory feed at all. So you can run inventory sync as often as you like, on its own
schedule, with no risk of it adding a listing. Adding and updating listings is only
ever the catalog flow.

---

## 3. Flow A: catalog sync (variations, then products)

Order matters, and the server enforces it: a product is only served once its variation
exists in Medusa and its variety image is live. So you pull in two steps and loop until
both are empty.

### Step 1: variations

```
GET /sync/variations?status=ready&limit=500
```

Response:

```json
{
  "type": "variations",
  "items": [
    {
      "external_id": "block_marble_breccia_oniciata_5ca3e544-...",
      "payload_hash": "9f3a...",
      "payload": {
        "branch": "block",
        "type": "Marble",
        "name": "Breccia Oniciata",
        "aliases": ["Breccia Oniciata Marble", "Marmo Breccia Oniciata"],
        "image_url": "https://.../variations/block_marble_breccia_oniciata_5ca3...png",
        "volume": "0.0014348"
      }
    }
  ]
}
```

For each item:
1. Upsert the variation in Medusa **by `external_id`** (store the `Key` as `external_id`).
2. Resolve the `type` **name** to your stone-type id. Names are canonical; they resolve.
3. Ack it (section 5) with the variation id Medusa minted or matched.

### Step 2: products

```
GET /sync/products?status=ready&limit=500
```

Only returns products whose variation is already `synced` **and** whose variety texture
image is live (so a product never lists without its image).

Response item payload:

```json
{
  "external_id": "POLONINE-7F3A2B19",
  "payload_hash": "1c80...",
  "payload": {
    "variation_external_id": "slab_travertine_walnut_8a1c...",
    "color": "Brown", "finish": "Honed", "quality": "First",
    "type": "Travertine", "category": "Slabs",
    "title": "Walnut Travertine Honed Slab",
    "description": "Walnut Travertine is a brown travertine ...",
    "handle": "walnut-travertine-honed-slab-polonine-7f3a2b19",
    "weight": 0.3, "length": 2.5, "width": 0.2, "height": 2.0,
    "origin_country_code": "IT",
    "company_id": "01KTV98X8RG743YR3QHCECZKKA",
    "sales_channel_id": "sc_01KTM2B2DJNSW6WPS1Q8FN8B2R",
    "bundle_size": 7,
    "ports": ["port_id_a", "port_id_b"],
    "image_urls": ["https://.../products/improved/polonine/<sha>.jpg", "..."]
  }
}
```

For each item:
1. Upsert the product **by `external_id`** (store the `SKU` as `external_id`).
2. Resolve `variation_external_id` (a `Key`) to the variation id you stored in step 1.
3. Resolve `color`/`finish`/`quality`/`category`/`type` **names** to your ids.
4. Ack it with the product id Medusa minted.

**Loop.** Re-pull both until they return no items. Because variations sync first, a
second pass picks up products that just became eligible. When both are empty, the
catalog is in sync.

---

## 4. Flow B: inventory sync (stock only, existing products only)

```
GET /sync/inventory?status=ready&limit=1000
```

Returns only the stock that **moved since the last sync**, and only for products that
are **already synced** (already in Medusa):

```json
{
  "type": "inventory",
  "items": [
    { "external_id": "POLONINE-7F3A2B19", "payload": { "sku": "POLONINE-7F3A2B19", "quantity": 7 } },
    { "external_id": "VARSHA-1C2D3E4F",   "payload": { "sku": "VARSHA-1C2D3E4F",   "quantity": 0 } }
  ]
}
```

For each item:
1. Find the Medusa product by `SKU` (the `external_id`) and **set its inventory
   quantity**. Do not create anything; if a SKU is not found, skip it (it should not
   happen, because only synced products are served).
2. Ack it (section 5). Inventory acks carry no `medusa_id`.

A `quantity` of `0` is a **reversible delist** (out of stock): the product stays in the
catalog, just unavailable. A later run that carries stock again simply sets it back.

---

## 5. What to send back: the ack (this is what keeps it in sync)

After applying an item, ack it:

```
POST /sync/ack
[
  { "type": "variations", "external_id": "block_marble_...", "medusa_id": "variation_01ABC...", "status": "created" },
  { "type": "products",   "external_id": "POLONINE-7F3A2B19", "medusa_id": "prod_01XYZ...",     "status": "updated" },
  { "type": "inventory",  "external_id": "VARSHA-1C2D3E4F",   "status": "updated" }
]
```

Response: `{ "acked": 3 }`.

Fields:
- `type`: `variations` | `products` | `inventory`.
- `external_id`: the `Key` (variations) or `SKU` (products, inventory) you were served.
- `medusa_id`: the id Medusa minted or matched. Required for `variations` and
  `products`; omit for `inventory`.
- `status`: `created` | `updated` | `skipped` (all treated as success) or `failed`.

What the ack does:
- **success** -> the pipeline records the `medusa_id` and marks the entity `synced`. It
  will not be served again unless its content changes. For inventory, success records
  the new stock as the last-synced level, so it stops being a delta.
- **failed** -> the entity is returned to the queue and offered again on the next pull.

Ack in batches as you apply. You do not have to ack the whole page at once.

---

## 6. The sync guarantee (why this stays in sync)

- **Pull-only.** The pipeline never writes into Medusa. You pull when you are ready. If
  Medusa is busy or down, the desired state simply waits in the ledger.
- **Ordered.** The server only serves an entity once its prerequisites are synced:
  a product after its variation is synced and its texture is live; stock after its
  product is synced. You cannot load out of order even if you pull naively.
- **Convergent.** An acked entity stops being served. Re-pulling moves only the delta.
  Pull each type until it returns empty and the two systems are exactly in sync.
- **Idempotent.** Every apply is an upsert **by `external_id`**, so re-applying the same
  item is a no-op. Each item carries a `payload_hash`; if it matches what you last
  applied, you may skip the write (optional, the upsert is already safe).
- **Observable.** `GET /sync/status` returns per-type counts of `pending` vs `synced`
  (and the inventory `delta` count), so you can confirm convergence and monitor drift:

```json
{
  "variation":  { "synced": 24749 },
  "product":    { "synced": 870, "pending": 12 },
  "inventory":  { "total": 870, "delta": 4 },
  "attribute":  { "synced": 115 }
}
```

When `pending` is 0 for variation and product, and `delta` is 0 for inventory, you are
fully in sync.

---

## 7. Ownership and safety (what the pipeline will never ask you to do)

- Only entities the pipeline owns are ever served (their `external_id` is our `Key`/`SKU`).
  **Products that website users listed in Medusa are never in these feeds**, so neither a
  catalog run nor an inventory run can ever touch a user-listed product.
- The only delist is **stock 0** (reversible). The pipeline never asks you to hard-delete.
- Uncertain entities are held, not served: a variety with no resolvable type, or a
  product whose texture is not live yet, is simply not in the feed until it is ready, so
  nothing lists broken.

---

## 8. Combinations (one decision to make together)

Valid combinations are the pre-computed priceable tuples (every `color x finish x quality`
per variety), about 2 million rows. They are pure id-tuples and need no ack. Today they
are delivered as a bulk file (`2_valid_combinations.csv`). Decide with the pipeline team
whether to keep bulk-loading them or add a `/sync/combinations` pull. Either way they
load after variations are synced (they reference variation ids).

---

## 9. Endpoint reference

| Method | Path | Purpose | Body / response |
|---|---|---|---|
| `GET` | `/sync/status` | per-type / per-state counts | response: `{type: {state: count}, ...}` |
| `GET` | `/sync/variations?status=ready&limit=N` | varieties to create/update | response: `{type, items:[{external_id, payload_hash, payload}]}` |
| `GET` | `/sync/products?status=ready&limit=N` | products to create/update (variation must be synced, texture live) | same shape |
| `GET` | `/sync/inventory?status=ready&limit=N` | stock deltas for already-synced products only | response: `{type, items:[{external_id, payload:{sku, quantity}}]}` |
| `POST` | `/sync/ack` | record minted ids, mark synced | body: `[{type, external_id, medusa_id?, status}]`; response: `{acked: N}` |

All requests require `Authorization: Bearer <token>`. `limit` is optional (page size).

---

## 10. The two jobs, end to end

**Catalog job** (on change / daily):

```
loop:
  v = GET /sync/variations?status=ready
  apply each, POST /sync/ack
  p = GET /sync/products?status=ready
  apply each, POST /sync/ack
until v and p are both empty
```

**Inventory job** (frequent, independent):

```
i = GET /sync/inventory?status=ready
for each: set the product's stock by SKU (never create), POST /sync/ack
```

Run them on whatever schedules suit you. They share nothing but the ledger, they are
both idempotent, and they both converge. That is the whole contract.
