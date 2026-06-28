# Sync ledger and pull-based Medusa integration: target design

Status: design only, not yet built. This document supersedes the sketch in
`MEDUSA_SYNC_PLAN.md` and is the build contract for the layer that replaces the
manual CSV import and export round-trip with a durable, env-aware sync between
the ingest pipeline and Medusa.

This is written to be read end to end before any code is written. It states the
data model, the writer model, the ordering guarantees enforced by serve-time
eligibility, the environment model, the ownership boundary that protects
user-listed products, the bootstrap-then-delta sync model with backward
corrections, the image promotion flow, the inventory lane, the backend
pull-and-apply contract (the part to share with the backend team, section 8), and
a phased, reversible migration with a concrete cutover test.

Integration direction: Medusa pulls read-only from the scraper when it is ready
and acks the ids it mints back. The scraper never writes into Medusa. This keeps
write pressure off the fragile backend and still gives the scraper every id it
needs.

No em dashes anywhere in this document or in the code it specifies (operating
principle 2 of `stone_pipeline_plan_v3_1.md`).

---

## 0. Why this exists (the root friction)

Combinations and products need Medusa internal ids (`variation_id`, attribute
`sourceid`s, category pcat ids). Those ids only exist after Medusa ingests the
variants. Today that forces a manual round-trip:

```
push variants CSV -> Medusa mints ids -> download variants_export.csv
  -> rebuild combinations and products against the fresh ids -> push those CSVs
```

Everything messy flows from that one cycle:

- `from_medusa/<env>/*.csv` (read side) and `to_upload/<env>/*.csv` (write side)
  are the two halves of a hand-run choreography.
- `catalog.verify_consistency()` exists only to catch when those CSV files drift
  out of sync with each other.
- The ordering (variants, then import, then export, then combinations, then
  products) lives in `SYNC_STEPS.md` and `RUNBOOK.md` as a human checklist, not
  as enforced code. The freeze rule (do not add variants mid-sync) is manual
  discipline with no guardrail.

The fix removes the round-trip and makes the ordering a property of the data,
not of a person following a list.

---

## 1. Core principles

1. **The Key is identity.** Every variety has a deterministic, Medusa
   independent `Key` (`{branch}_{type}_{name}_{uuid5(path)}`, `core/ids.py` and
   `stages/curate.py`). The Key is stable across runs and across dev and prod.
   Everything joins on Key. Medusa stores the Key as `external_id` and never
   changes it.
2. **The ledger is the scraper-side system of record.** A small per-environment
   database holds every entity (variation, attribute, combination, product,
   inventory, image, gap), its last-known Medusa id, a content hash, and a sync
   state. The pipeline reads the ledger, never Medusa export CSVs.
3. **Medusa is the authority for id values, not a writer of the store.** The id
   originates in Medusa, but it travels back as data in the ack Medusa posts when
   it applies a pull, and the scraper-side sync service performs the write into
   the ledger. Medusa has no connection to the ledger and does not know it exists.
   See the writer model (section 3).
4. **Medusa pulls; the scraper never pushes into it.** Stages produce canonical
   rows and upsert them into the ledger. A read-only sync service exposes the
   ledger's desired state, and Medusa pulls and applies at its own pace, acking
   the ids it mints back to the ledger. The scraper makes no write into Medusa;
   its only outbound call is an optional read-only attribute refresh. This keeps
   write pressure off the fragile system.
5. **Ordering is enforced by entity state, not by a checklist.** The engine runs
   in dependency phases with barriers. A phase cannot start until every entity
   of the prior phase reached `synced`. This is the "each step completes before
   moving on" requirement, made structural.
6. **Idempotent and resumable everywhere.** Upsert by Key plus a content hash
   means a re-run pushes only what changed and resumes from the entities not yet
   `synced`, never from the top.
7. **Source agnostic.** Anything that yields `CanonicalRow`s is a source: a
   scraper, a supplier API, a manual CSV drop, an EDI feed, a partner database.
   The ledger and the engine never know which. Growth is one adapter plus one
   config block, never a stage or sync change.

---

## 2. Environment model: one explicit RunContext

The module serves both dev and prod Medusa. The run must know which side it is
on, with no silent default.

- The deployment is already split per env: dev and prod run as separate ECS
  deployments (not one runtime-toggled task, per the infra split), so each running
  stack is pinned to one environment by its infrastructure. The residual footgun
  is that `BLOKPORT_ENV` still defaults to `development` in `config/settings.py`.
  That default is removed and environment becomes a required input, so a
  misconfigured task aborts before any work rather than silently acting as dev.
- Environment resolves once into a `RunContext` object that is the single source
  for everything env-specific. Every stage and the sync service reads from it.
  Nothing reads `BLOKPORT_ENV` ad hoc anywhere else.

`RunContext` carries, per environment:

| field | meaning |
|---|---|
| `env` | `development` or `production`, explicit |
| `ledger_dsn` | the dev ledger or the prod ledger connection |
| `sync_service_token` | token Medusa's pull job uses to call this env's sync service (from SSM SecureString) |
| `medusa_read_url`, `medusa_read_token` | optional, read-only, for the attribute refresh (section 8.5) |
| `s3_staging_bucket` | image staging bucket for this env |
| `s3_live_bucket` | image live bucket for this env (what Medusa serves) |
| `company_id`, `sales_channel_id` | owner ids for this env |
| `category_pcats` | Slabs, Blocks, Tiles pcat ids for this env |
| `backend_id_fingerprint` | hash of the live id set, for the re-seed guard |

**Cross-env guard (hard).** Before any write to the ledger or any push to
Medusa, assert `RunContext.env == ledger.env == live Medusa fingerprint env`. A
dev run cannot touch the prod ledger or push to prod Medusa, and vice versa,
even if a credential is misconfigured.

**One env per run, never combined.** A run targets exactly one environment.
There is no combined dev and prod run, and no shared cross-env state. Each
environment is a fully isolated stack: its own ledger, its own staging and live
buckets, its own Medusa ids, its own reconcile passes. Dev and prod share only
the env-independent Keys, and therefore can reuse image bytes by an explicit
one-direction copy (section 6A), nothing else. This isolation is the thing that
keeps two environments from becoming a mess to operate: you reason about one
stack at a time.

---

## 3. The writer model (who writes the ledger, and when)

This is the heart of the substrate decision, so it is stated precisely.

The ledger has exactly one software actor that writes it: the scraper-side
pipeline. It writes in two distinct moments:

1. **Ingest write.** After the processing stages, each canonical entity is
   upserted into the ledger by Key (or by SKU for products). New entities land
   as `pending` with a null Medusa id. Changed entities are marked `dirty` by
   comparing a freshly computed `payload_hash`.
2. **Ack write-back.** When Medusa pulls and applies an entity, it acks the
   minted or matched id to the scraper's sync service, which writes that id into
   the ledger and moves the entity to `synced`. The ack handler is scraper-side,
   so the scraper is still the only software that writes the store.

Both writes are scraper-side. Medusa is never a writer. So the store is
single-actor at the level of "what software touches the database file or
server."

The genuine concurrency question is whether two scraper-side processes write at
the same time. Two cases can produce that:

- two source ingests running in parallel (the corpus will grow, and parallel
  ingest is a likely optimization), and
- a reconcile pass running while an ingest runs.

If those are serialized (one orchestrated run at a time per env), a single-file
embedded database is safe. If they can overlap, a server database with row-level
locking is the safer substrate. This is the only real input to the substrate
choice, and section 12 resolves it.

---

## 4. The ledger schema

One database per environment (a dev ledger and a prod ledger), because Medusa
ids differ per env. This mirrors the existing `from_medusa/<env>` split. All
tables carry the `env` so a misrouted connection fails the cross-env guard
rather than corrupting data.

Tables (columns abbreviated; every table has `created_at`, `updated_at`):

`variation`
- `key` (PK), `branch`, `type`, `name`, `aliases`, `image_url`
- `image_model` (the generator of the current variant texture, e.g.
  `flux2-max-v1`; null if none), `image_sha256`
- `medusa_id` (nullable until synced)
- `payload_hash`, `state`, `first_seen`, `last_synced`

`attribute`
- `category` (color, finish, quality, type, category), `value` (PK with
  category), `medusa_id`, `state`
- this table is refreshed from Medusa, not minted by the pipeline; attributes
  are a controlled vocabulary the backend owns

`combination`
- `combo_key` (PK) = hash(`variation_key`, color, finish, quality)
- `variation_key` (FK), `color`, `finish`, `quality`
- `medusa_id` (nullable), `state`

`product`
- `sku` (PK) = `{source_code}-{surrogate_key}` uppercased
- `source`, `variation_key` (FK), `color`, `finish`, `quality`
- `dims` (weight, length, width, height), `image_keys`, `origin_country_code`
- `payload_hash`, `medusa_id` (nullable), `state`, `last_synced`

`inventory`
- `sku` (PK, FK to product), `qty`, `last_synced_qty`, `updated_at`

`image`
- `sha256` (PK), `source_url`, `staging_key`, `live_key` (nullable)
- `state` in {staged, promoted}
- one row per unique image content; the product to image link lives on the
  product row as an ordered list, never inferred from a filename

`gap`
- `kind` (missing_variation, missing_leaf_child, missing_attribute), `name`,
  `nearest_existing`, `nearest_score`, `example_url`, `state` in {open, resolved}
- the durable form of the per-run `tree_gaps.csv`, deduped across runs

`sync_run`
- `id`, `env`, `fingerprint`, `started`, `finished`, per-phase counts, status
- the audit ledger; one row per reconcile pass

State enum, shared by `variation`, `combination`, `product`:
`pending -> dirty -> syncing -> synced`, plus `needs_resync` (set on a fingerprint
change, see section 9) and `gap_held` (cannot sync, an open gap blocks it).

The per-run `canonical.parquet` checkpoint stays exactly as it is. The ledger
replaces the cross-system CSV files, not the processing artifacts.

---

## 5. The sync service: eligibility-ordered serving, Medusa pulls

The scraper does not push into Medusa. It exposes the ledger's desired state
through a read-only sync service, and Medusa pulls and applies at its own pace
(the full backend contract is section 8). Medusa is the fragile system, so nothing
writes into it from outside: it reads when it is ready, applies, and acks the ids
it minted back to the scraper, which records them in the ledger. This is how the
scraper gets the ids it needs without an export download, and how the ordering
holds without holding a transaction open against Medusa.

Dependency order is enforced by an eligibility filter on the read side, not by a
pusher. An entity is served as `ready` only when its prerequisites are `synced`:

```
scrape or other ingest -> processing stages -> UPSERT entities into the ledger
  (compute payload_hash, mark new as pending and changed as dirty)

scraper sync service (read-only over the ledger, plus an ack writer):
  serves only entities that are ready, in dependency-safe order:
    variations    ready immediately (attributes already exist in Medusa)
    combinations  ready once their variation is synced
    products      ready once variation + combination synced and images promoted
    inventory     ready once its product is synced
  on each ack {key|sku, medusa_id}: write the ledger, mark the entity synced

Medusa pull-and-apply job (the only writer into Medusa): see section 8
  pulls each ready batch -> upserts by external_id -> acks the minted ids back
```

Properties:

- **No push, pull when ready.** Medusa is never written into from outside. If it
  is busy or down, desired state waits in the ledger until the next pull. This is
  the protection the fragile system needs.
- **The scraper still gets every id.** Each applied entity is acked with the id
  Medusa minted or matched; the scraper writes it into the ledger, which therefore
  mirrors Medusa. No export download.
- **Eligibility is the barrier.** A dependent is never served before its
  prerequisite is `synced`, so "complete one step before the next" holds even
  though Medusa pulls on its own schedule. Acking a batch is what flips its
  entities to `synced` and so unlocks the dependents on the next pull.
- **No freeze rule.** A variation discovered mid-run sits at `pending` and is
  served on a later pull, never lost.
- **Held entities are never served.** A `gap_held` variation, or any entity
  blocked by an open gap, is excluded until the gap is resolved. Its dependents
  are held with it. An unresolved gap can never stall the sync.
- **Owned only.** The service only ever serves entities the scraper owns. Foreign,
  user-listed products are never in the desired state, so Medusa is never asked to
  touch them (section 5A).
- **Resumable.** A `failed` ack leaves the entity `dirty`; the next pull offers it
  again. One bad entity never blocks the batch.
- **Dry-run and consistency are queries.** The service can report the full diff
  (what is ready, what would change, what would orphan) without Medusa pulling, and
  the old `catalog.verify_consistency()` cross-file checks become ledger
  invariants: a product whose variation has no `medusa_id` is simply not `ready`.

The existing trust ladder is preserved. `certify.py`, the health gate, and the
module gates (`gates/`) all still run during ingest. The sync service refuses to
serve entities from a source that did not pass its gates or is not `mode: auto`.

---

## 5A. Ownership boundary: never touch what we do not own

Medusa also holds products that website users listed themselves. The engine must
never modify, delist, or delete any of them. Ownership is the hard boundary on
everything the engine does, the product-level equivalent of the cross-env guard.

An entity is ours if, and only if, one of these holds:

- it carries our `external_id` (our Key, or our SKU for products), or
- its `company_id` equals the `RunContext.company_id` for this env (the per-source
  owning account).

Everything else in Medusa is foreign and invisible to the engine:

- The engine only ever upserts entities that exist in our ledger. It never
  creates, updates, or deletes by any other selector.
- **Orphan detection is scoped to owned entities only.** An orphan is an entity
  that is `synced` in our ledger but no longer in our current catalog. A foreign
  product is never an orphan, because it was never in our ledger. So a missing
  catalog row can never cause the engine to touch a user-listed product.
- The orphan action is the soft, reversible one: set the owned product to stock 0
  (the discontinued path, section 7), never a hard delete.

Before any delist or orphan step, the engine asserts the target carries our
`external_id` or `company_id`. A target that fails the assertion is skipped and
logged, never acted on. This is a structural refusal, not a convention, so adding
sources or scaling the catalog can never put user-listed products at risk.

---

## 5B. Bootstrap full load, then deltas, and backward corrections

This is the explicit contract for "load the full set once, then only update,"
and for keeping the ledger and Medusa in sync without ever reloading everything.

**Bootstrap (one time, per env).** The first sync is a full load. The ledger is
seeded from the current Medusa state (the existing full variants and combinations
export), so every owned entity starts `synced` with its real Medusa id. If a set
is loaded fresh instead of seeded, the first reconcile pass pushes everything
once: all entities are `pending`, the phases run in order, and the ledger captures
the minted ids. Either way, after the bootstrap the ledger mirrors Medusa for
every owned entity.

**Steady state (every run after).** A scraper run does not reload anything. It
recomputes each entity's `payload_hash` and marks only the changed ones `dirty`
and the genuinely new ones `pending`. The sync service serves exactly that delta
for Medusa to pull. An unchanged entity stays `synced` with a matching hash and is
never served: no load, no churn. This is what keeps the database and Medusa in
sync without a full reload, and why steady-state sync is cheap. The full variant
and combination set is loaded once at bootstrap and only ever updated thereafter.

**Backward corrections.** A correction to an already-synced entity (a fixed
alias, a corrected attribute or dimension, a manual override, a re-matched
variation) recomputes its `payload_hash`. The new hash differs, so the entity
flips `synced -> dirty` and the next reconcile pass re-pushes it as an update
through the same upsert-by-Key path. Corrections use the exact same machinery as
new data; there is no separate correction flow, and the `sync_run` audit records
every update so a correction is traceable.

A correction that must remove an owned entity created in error (a wrong variant)
is an explicit retire action: mark the ledger entity retired, and the engine
applies the owned-only orphan action (stock 0, or a backend-confirmed delete only
if that is ever brought into scope). Removal is never an implicit delete
triggered by mere absence from a scrape, which protects against a partial scrape
wiping good data (the same intent as the >30% delist guard, section 7).

---

## 6. Image staging to live promotion

Per env there are two buckets: a staging bucket (for the `scraped/` and
`improved/` processing artifacts and the generated `{Key}.png` variant images)
and a live bucket (what Medusa serves). The `image` table tracks the crossing.

- Processing writes to staging and records `staging_key`, `state = staged`. The
  key is the content hash, so the same image is the same key and is never
  duplicated. This preserves the existing idempotency (manifest skip, content
  key dedup, exists backstop), now as a table instead of `_manifest.json`.
- The scraper promotes an image (copy staging to live, set `live_key`,
  `state = promoted`) just before it serves the product that references it as
  `ready`. Promotion is a prerequisite for product readiness, so nothing reaches
  the live bucket before the product that needs it is eligible to load, and the
  product payload carries the live url.
- A re-run with no image change is a no-op: the image is already `promoted`.

Both image lanes (faithful scraped photos and generated variant textures) use
the same staging then promote crossing, so the live bucket only ever holds
images tied to a synced product.

---

## 6A. Variant image generation: generate once, upgrade once, never repeat

The generated variant-texture lane (`image_pipeline/`, one `{Key}.png` per
variety via the fal.ai FLUX API, about $0.10 per image) is the expensive lane.
The database must drive a strict generate-decision so a run never pays to
regenerate an image it already has at the current quality.

The decision is keyed on the variety Key and the model version that produced the
current texture. The `variation` row carries `image_model` (the generator of the
current image, e.g. `flux2-max-v1`, null if none) and `image_sha256`. Config
holds `CURRENT_VARIANT_IMAGE_MODEL`, the version string of the good API.

For each product-backed variant Key, the generation lane decides:

1. `image_model` is null (a new variant, no texture yet) -> GENERATE, then set
   `image_model = CURRENT_VARIANT_IMAGE_MODEL`.
2. `image_model` is set but not equal to `CURRENT_VARIANT_IMAGE_MODEL` (an
   existing variant whose texture came from an older, lower-quality method) ->
   REGENERATE ONCE to upgrade, then set `image_model = CURRENT_VARIANT_IMAGE_MODEL`.
3. `image_model` equals `CURRENT_VARIANT_IMAGE_MODEL` (already best quality) ->
   SKIP. Never regenerate. This is the cost guard: a variant is paid for at most
   once per model version.

So a new variant is generated, an existing variant on an old method is upgraded
exactly once, and a variant already on the current API is never touched again.
Bumping `CURRENT_VARIANT_IMAGE_MODEL` is the one deliberate trigger for a
fleet-wide one-time re-upgrade; nothing else regenerates. This formalizes the
existing `catalog_source/image_model.csv` audit (Key to model) into ledger state.

### Generated images go to the runner's bucket, and dev and prod stay isolated

Generation runs for the one environment the run targets (section 2). The bytes
go to that environment's bucket, the bucket of whoever ran the system, as
required. The generate-decision reads that same environment's ledger
`image_model`. There is no shared cross-env generation registry, because a single
live system spanning both environments is exactly the mess to avoid.

A variant texture is env-independent (same Key, same bytes, PIPELINE_OVERVIEW
section 5), so dev and prod can reuse the same image without paying the API twice.
That reuse is an explicit one-direction promotion, not a shared store: when
promoting dev to prod, copy the `{Key}.png` bytes from the dev bucket to the prod
bucket (a cheap S3 copy) and set the prod ledger `image_model` to the same
version, so prod never regenerates what dev already generated. With a dev-first
workflow, prod generation then fires only for the rare Key that exists in prod but
never passed through dev.

This keeps three things true at once: generated images land in the runner's
bucket, the expensive API is paid at most once per Key per model version, and the
two environments never run as one combined system.

---

## 7. Inventory lane

Inventory becomes a first-class entity with a fast lane that needs no product
re-import and no export download.

- An `inventory_only` ingest updates `inventory.qty` in the ledger.
- Inventory is served as `ready` only where `qty != last_synced_qty`; on the ack
  the scraper sets `last_synced_qty = qty`. Unchanged stock is never served, so a
  stock refresh moves only the deltas.
- The >30% delist guard becomes a count query the sync service applies before it
  serves inventory, refusing to expose a mass delist from a partial scrape,
  exactly as today but as a query instead of a CSV diff.
- Discontinued products (supplier dropped them) set `qty = 0`, a reversible
  delist, recorded for audit.

---

## 8. Backend integration contract: Medusa pulls and applies

This section is the shared contract for the backend team. It defines how Medusa
reads desired state from the scraper and loads it into itself, at its own pace.
The scraper never writes into Medusa. Medusa pulls when it is ready, applies, and
acks the ids it minted back to the scraper. This protects Medusa, the most
fragile system, from external write pressure, and still gives the scraper the ids
it needs through the ack channel.

### 8.1 The shape of the integration

```
scraper ledger (desired state, keyed by Key/SKU)
   ^                         ^
   | GET ready batch         | POST ack {key, medusa_id, status}
   |                         |
Medusa pull-and-apply job (runs on Medusa's own schedule)
   for each entity type, in dependency order:
     pull the ready batch -> upsert into Medusa by external_id -> ack ids back
```

- The scraper exposes a small read-only sync API over the ledger. It serves only
  entities that are eligible to load now: owned, not held by a gap, and with every
  prerequisite already applied. It never serves a dependent before its
  prerequisite, so Medusa's job cannot load out of order even if it pulls naively.
- Medusa runs the pull-and-apply job whenever it is ready. There is no push, no
  deadline, and no external transaction held open against Medusa. If Medusa is
  busy or down, desired state simply waits in the ledger until the next pull.
- Each applied entity is acked back with the id Medusa minted or matched. The
  scraper writes that id into the ledger, so the ledger mirrors Medusa and the
  scraper has every id it needs for diagnostics, consistency, and image linking.

### 8.2 The endpoints the scraper exposes (read plus ack only)

All are authenticated with a token Medusa holds, env-scoped, and read-only except
the ack, which writes only the scraper's own ledger, never Medusa.

`GET /sync/<type>?status=ready&limit=N&cursor=...`
- `<type>` in `variations`, `combinations`, `products`, `inventory`.
- Returns a page of entities of that type that are ready to load now: owned, not
  held by an open gap, and with every prerequisite already `synced`. Eligibility
  is computed server-side (section 8.4), so the caller does not reason about
  dependencies.
- Each item carries its `Key` (or `SKU`), the full payload to load (section 8.3),
  and a `payload_hash` so Medusa can skip an entity it already applied at that
  hash.

`POST /sync/ack`
- Body: a list of `{key|sku, medusa_id, status: created|updated|skipped|failed,
  error?}`.
- The scraper writes `medusa_id` into the ledger and moves the entity to `synced`,
  or records `failed` with the error and leaves it `dirty` for the next pull. This
  is the only write the integration performs, and it writes the scraper ledger,
  not Medusa.

`GET /sync/status`
- Summary counts per type and state (pending, dirty, ready, synced, held, failed),
  for Medusa's job and for human monitoring.

The scraper may also read Medusa's attribute list read-only to refresh attribute
ids in the ledger (section 8.5). That is the only direction in which the scraper
reads from Medusa, and it is read-only.

### 8.3 Entity payloads (keyed by Key, attributes by canonical name)

Every payload is addressed by the stable `Key` (Medusa stores it as `external_id`)
and references other entities by Key or by canonical attribute name, never by a
Medusa id. Medusa resolves names and Keys to its own ids at load time, because
Medusa owns those ids. This is why the scraper does not need ids in advance and
why a re-seed needs no special handling.

- `variation`: `external_id` = Key, `branch`, `type` (canonical name), `name`,
  `aliases[]`, `image_url` (a live-bucket url), `volume`. Medusa upserts by
  `external_id`, mints or matches the variation id, acks it.
- `combination`: `external_id` = combo_key, `variation_external_id` = variation
  Key, and the canonical `color`, `finish`, `quality` names. Medusa resolves the
  variation by Key and the attributes by name, upserts, acks its id. Served only
  after the variation is `synced`.
- `product`: `external_id` = SKU (`{source_code}-{surrogate_key}`),
  `variation_external_id` = variation Key, canonical `color`/`finish`/`quality`
  names, `title`, `description`, `handle`, dims (`weight,length,width,height`),
  `origin_country_code`, `image_urls[]` (ordered live-bucket urls), `company_id`,
  `sales_channel_id`, `category` (canonical), `bundle_size`, ports. Served only
  after the variation and its combination are `synced` and its images promoted.
- `inventory`: `sku`, `quantity`. Served only after the product is `synced`. A
  `quantity` of 0 is the reversible delist for a discontinued product.

Booleans and casing follow the existing template conventions. The canonical
attribute names are exactly the backbone spellings the scraper already validates
against (the clean gate guarantees canonical spelling), so a name always resolves
in Medusa.

### 8.4 Ordering: how "wait for one step before the next" is enforced

Ordering is enforced by the scraper's eligibility filter, not by Medusa. An entity
is served as `ready` only when all of its prerequisites are `synced`:

| type | becomes ready when |
|---|---|
| variation | always (attributes already exist in Medusa) |
| combination | its variation is `synced` |
| product | its variation and combination are `synced`, images promoted |
| inventory | its product is `synced` |

So Medusa can pull each type and apply, and it will never receive a combination
whose variation is not yet in Medusa, or a product whose combination is missing.
Acking a batch is what flips its entities to `synced`, which is what unlocks the
dependents on the next pull. The "complete one step before the next" guarantee
holds even though Medusa pulls at its own pace, because the gate is the data,
served only when safe. A held entity (an open gap) is never served until the gap
is resolved.

### 8.5 Idempotency, ownership, re-seed, failure

- **Idempotency.** Every load is an upsert by `external_id`. Re-pulling and
  re-applying the same entity at the same `payload_hash` is a no-op Medusa can
  skip. The scraper never sends an insert that could duplicate.
- **Ownership (critical).** Medusa applies these loads under the scraper's
  ownership (`external_id` present, the scraper's `company_id`). A pull must never
  overwrite or delete a product a website user listed under different ownership.
  The scraper never asks Medusa to touch a foreign product: orphaning is scoped to
  owned entities only (section 5A), and the delist action is stock 0, never a hard
  delete.
- **Re-seed.** Because every reference is by Key or canonical name, a re-seeded
  Medusa re-resolves everything on the next pull. The scraper marks ids stale
  (section 9) and the acks repopulate them. No coordinated migration.
- **Failure and retry.** A `failed` ack leaves the entity `dirty`, so the next
  pull offers it again. A persistently failing entity surfaces in `GET
  /sync/status` and the run summary. One bad entity never blocks the batch: Medusa
  acks the good ones and reports the bad ones.

### 8.6 What the backend builds (the checklist to hand over)

1. A pull-and-apply job, on Medusa's own schedule, that for each type in order
   (`variations`, `combinations`, `products`, `inventory`) pulls the ready batch
   from `GET /sync/<type>`, upserts into Medusa by `external_id`, and acks the
   minted ids back to `POST /sync/ack`.
2. `external_id` storage and upsert-by-`external_id` for variations, combinations,
   products, and inventory, resolving variation Keys and canonical attribute names
   to Medusa ids server-side at load (server-side resolution is required in this
   model, because Medusa is the one applying the load).
3. Ownership enforcement: never overwrite or delete a product outside the
   scraper's ownership; honor stock 0 as the only delist.
4. Idempotent skip when `payload_hash` is unchanged.
5. The per-env token and network path to reach the scraper sync API (section 10).

The scraper builds the read-only sync API, the eligibility filter, and the ack
writer. Neither side pushes into the other's store: Medusa pulls desired state,
the scraper records acked ids.

---

## 9. Re-seed and fingerprint handling

When a Medusa environment is re-seeded, every internal id changes but every Key
survives. The ledger absorbs this without a manual re-pin.

- `RunContext.backend_id_fingerprint` is checked against the live backend at
  startup. On a mismatch, the engine does not abort. It marks all `medusa_id`
  columns in that env's ledger stale and sets the affected entities to
  `needs_resync`.
- The next pull re-resolves every id as Medusa applies each entity by Key and
  canonical name, and the acks repopulate the ledger. Keys carry the identity
  across the re-seed, so this is automatic.
- The `sync_run` row records the fingerprint change as an explicit event.

This is cleaner than today's abort and re-pin, and it is the same mechanism that
promotes dev to prod: point `RunContext` at the prod ledger and prod sync service,
and the first pull resolves prod ids by the same Keys.

---

## 10. Cross-ECS access, networking, secrets

The scraper, Medusa dev, and Medusa prod run on different ECS clusters. Because
Medusa pulls, the integration hop is now inbound to the scraper rather than
outbound to Medusa.

- Each env's Medusa cluster calls the scraper's read-only sync service over HTTPS.
  So the scraper exposes one authenticated, env-scoped HTTPS endpoint reachable by
  the matching Medusa cluster, and nothing else. The endpoint is read-only plus the
  self-ack; it can never write into another system. This is the one new piece of
  network surface the pull model adds.
- The scraper still needs egress to S3 (staging and live, per env) and, optionally,
  read-only egress to Medusa for the attribute refresh (section 8.5).
- A token per env authenticates the sync service. Medusa's pull job holds it; it
  lives in SSM SecureString, env-scoped, reusing the existing SSM and KMS pattern
  (`FAL_KEY`, scraper proxy).
- Medusa reads desired state only through the sync API, never the raw database, so
  there is no cross-account database access and no schema coupling. If the
  substrate is a server database, it sits in the scraper VPC behind the service and
  only the scraper task connects to it.

Net posture: the scraper gains one authenticated inbound HTTPS endpoint (the sync
service), scoped per env, and keeps egress-only for S3 and the optional attribute
read. Medusa holds a per-env token and pulls; it never receives a write.

---

## 11. Source-agnostic ingest (growth)

The `AdapterBase` boundary already makes any data source a first-class source.
The ledger and the sync service never know whether canonical rows came from a
scraper, a supplier API, a manual CSV drop, an EDI feed, or a partner database.

- A new source signs one contract: produce `CanonicalRow`s and declare a health
  contract for the Stage 0 gate.
- Nothing downstream of the adapter changes when a source is added. The ledger
  schema, the engine phases, and the Medusa contract are all source-independent.
- The mental model is "ingest source," not "scraper." The naming is cosmetic; the
  architecture is already correct for growth.

---

## 12. Substrate decision (SQLite vs Postgres), tied to the writer model

The writer model (section 3) is the deciding input, not whether Medusa supplies
ids (it does, but only as response data the engine writes).

- If ingest and the ack writer run serialized, one orchestrated run at a time per
  env, an embedded single-file database (SQLite on EFS, mounted by the scraper
  task) is safe, cheap, and zero-ops. It is the right substrate for the Phase 1
  prototype, which is single-process and write-through with no Medusa calls.
- The corpus will grow and parallel ingest is a likely optimization, and the sync
  service's ack writes can land while an ingest is running. Concurrent
  scraper-side writers want a server database with row-level locking. A small RDS
  Postgres in the scraper VPC is the right target once the sync service is live and
  serving a pulling Medusa.

Recommendation: build behind a thin data-access layer so the substrate is one
swap. Prototype on SQLite on EFS (Phase 1 below). Graduate the same schema to RDS
Postgres when the sync service goes live and concurrency appears. The schema and
the code do not change across the swap; only the connection does. A server
database also makes the sync service easier to expose as a small HTTPS endpoint in
the scraper VPC.

Open decision for the user: graduate to Postgres at Phase 3 (when the sync service
first serves a live, pulling Medusa and writes acked ids back, so the store is
already concurrent-safe), or run on SQLite through the full migration and swap only
when parallel ingest is actually turned on. Default recommendation: swap at
Phase 3.

---

## 13. Migration plan (phased, reversible, with a concrete cutover test)

Each phase is independently reversible and keeps the file flow as a fallback,
matching the "keep testing with the file flow" stance. The cutover criterion is
automatable, not a judgment call.

**Phase 1: write-through ledger (no Medusa calls).** Build the ledger schema and
a write-through data-access layer. The pipeline keeps emitting the current CSVs
and also upserts every entity into the per-env ledger. Seed the ledger once from
the current `from_medusa/<env>` exports and the unified variant list
(`variants_export_base.csv`, now kept identical to `1_variants_full.csv` by
`sync_variants_base.py`, so it is the natural single seed for variations).

Cutover test for Phase 1: render the ledger back out into the existing
`to_upload` CSV shapes (`1_variants_*`, `2_valid_combinations`, `3_products_*`,
`4_inventory_update`) and assert it is byte-identical, or fully diff-explained,
to what the CSV flow produces, across several dev runs. When that holds, the
ledger is proven to carry the same truth as the CSVs.

**Phase 2: sync service read-only, backend pull job in dry-run.** Stand up the
read-only sync service over the seeded ledger. The backend builds the pull-and-
apply job (section 8) against dev and runs it in dry-run: pull ready batches,
apply into a scratch space or log only, do not commit. Compare what the service
would serve, and what the job would apply, against the CSV flow until trusted.

**Phase 3: variations live.** Let the backend pull job apply variations for real
on dev for one source behind `mode: auto`; the acks write the variation ids into
the ledger. The export download for variants is now gone. The CSV flow stays as
fallback. Recommended substrate swap to RDS Postgres here.

**Phase 4: combinations, images, products, inventory live.** Let the pull job
apply the remaining types, served in eligibility order off ledger state. Retire
the `to_upload` choreography once the pull job drives the full sync for the proven
sources.

**Phase 5: retire `from_medusa` reads.** The pipeline reads attribute and
variation ids from the ledger (attributes refreshed by the read-only attribute
pull, section 8.5; variation ids written by acks), not from downloaded export
CSVs. The ledger is now the boundary. The CSV export of the ledger can remain as a
debug artifact.

At every phase, retire a CSV only after its ledger-driven equivalent is proven by
the Phase 1 equivalence test for that artifact. Nothing is deleted on faith.

---

## 14. What is kept and what is retired

Kept:

- the deterministic Key scheme (`core/ids.py`, `stages/curate.py`)
- the per-run `canonical.parquet` checkpoint and provenance
- the resolvers and the matching engine
- the gates (`gates/`), `certify.py`, the health gate, the trust ladder
- the image content-hash idempotency (now an `image` table)

Retired, in the phase order above:

- `from_medusa/<env>/*.csv` reads
- the `to_upload/<env>/*` choreography
- `SYNC_STEPS.md` as a human procedure (the pull job and eligibility serving
  replace it)
- `catalog.verify_consistency()` CSV set-diffs (now a ledger invariant)
- `_manifest.json` (now the `image` table)

---

## 15. Open decisions for the user

1. Substrate graduation point: swap SQLite to RDS Postgres at Phase 3
   (recommended), or run SQLite through the full migration and swap only when
   parallel ingest is enabled.
2. Confirm with the backend chat that they can build the pull-and-apply job
   (section 8): upsert by `external_id`, resolve variation Keys and canonical
   attribute names to Medusa ids server-side at load, and ack the minted ids back.
   Server-side resolution by Key and name is required in the pull model, because
   Medusa is the one applying the load.
3. Whether an atomic transactional sync endpoint is wanted later (section 8,
   optional), or the resumable per-entity phase model is sufficient (it is, for
   correctness; this is a preference about all-or-nothing semantics).
4. Per-source graduation order to `mode: auto` live sync, starting from the
   already-proven sources (varsha, polonine, marenostone).
5. The dev to prod image copy (section 6A) needs the prod promotion step to read
   the dev bucket. If the two env buckets are in different AWS accounts, grant
   cross-account read on the dev bucket for the prod runner. This is the only
   cross-env access the design needs, and it is a copy, not a shared live system.
6. The value of `CURRENT_VARIANT_IMAGE_MODEL` and the policy for bumping it (a
   bump triggers a one-time fleet re-upgrade of every variant still on an older
   method, per env).
7. Orphan policy for owned products that leave the catalog (sections 5A, 5B):
   confirm stock 0 (reversible, recommended) is the only action, or whether a
   backend-confirmed hard delete is ever wanted. Foreign user-listed products are
   out of scope either way, never touched.
8. The pull cadence and page size for the backend job (how often Medusa pulls,
   how many entities per page), and the authentication and network path for the
   inbound sync service per env (section 10). Confirm with the backend chat
   alongside decision 2.
