# Sync ledger and reconcile engine: target design

Status: design only, not yet built. This document supersedes the sketch in
`MEDUSA_SYNC_PLAN.md` and is the build contract for the layer that replaces the
manual CSV import and export round-trip with a durable, env-aware sync between
the ingest pipeline and Medusa.

This is written to be read end to end before any code is written. It states the
data model, the writer model, the ordering guarantees, the environment model,
the image promotion flow, the inventory lane, the Medusa-side contract, and a
phased, reversible migration with a concrete cutover test.

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
   originates in Medusa, but it travels back as data in the Admin API response,
   and the scraper-side reconcile engine performs the write into the ledger.
   Medusa has no connection to the ledger and does not know it exists. See the
   writer model (section 3).
4. **The reconcile engine is the only component that talks to Medusa.** Stages
   produce canonical rows and upsert them into the ledger. One engine walks the
   ledger and syncs it to Medusa over the Admin API. Nothing else makes an
   outbound call to Medusa.
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

- Today `BLOKPORT_ENV` defaults to `development` in `config/settings.py`. That
  default is removed. Environment is a required input; an unset or unknown value
  aborts before any work.
- Environment resolves once into a `RunContext` object that is the single source
  for everything env-specific. Every stage and the reconcile engine reads from
  it. Nothing reads `BLOKPORT_ENV` ad hoc anywhere else.

`RunContext` carries, per environment:

| field | meaning |
|---|---|
| `env` | `development` or `production`, explicit |
| `ledger_dsn` | the dev ledger or the prod ledger connection |
| `medusa_api_url` | Admin API base URL for this env (from SSM) |
| `medusa_admin_token` | Admin API token for this env (from SSM SecureString) |
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
2. **Reconcile write-back.** The reconcile engine pushes a `pending` or `dirty`
   entity to Medusa, reads the minted or confirmed id out of the Admin API
   response, and writes that id back into the ledger, moving the entity to
   `synced`.

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

## 5. The reconcile engine: phase-ordered sync

The engine reads the ledger and pushes to Medusa in dependency phases. Each
phase is a barrier: it does not start until every entity of the prior phase is
`synced`. This is the ordering guarantee.

```
scrape or other ingest -> processing stages -> UPSERT entities into the ledger
  (compute payload_hash, mark new as pending and changed as dirty)

reconcile engine (the only component that talks to Medusa):
  Phase 1  attributes   refresh ids from Medusa into the ledger
  Phase 2  variations   upsert by Key; read minted id from response; write back
           barrier: every variation in this batch is synced
  Phase 3  combinations build from the ledger (ids now present); upsert; write back
           barrier: every combination synced
  Phase 4  images       promote staging to live for images products will reference
           barrier: referenced images promoted
  Phase 5  products     upsert by Key; reference variation_id and combination
           barrier: every product synced
  Phase 6  inventory    push deltas where qty != last_synced_qty
```

Properties:

- **No round-trip.** Phase 2 reads the minted id straight out of the upsert
  response and writes it to the ledger. There is no export download. By Phase 3
  every needed id is already in the ledger.
- **No freeze rule.** A variation discovered mid-run sits at `pending`. It is
  picked up in the next Phase 2, not lost. The barrier guarantees combinations
  and products only build against variations that already have ids.
- **Resumable.** A failure mid-phase leaves entities in `syncing` or still
  `dirty`. A re-run continues from there. The phase barriers make partial
  progress safe.
- **Dry-run first.** The engine has a dry-run mode that computes the full diff
  (create, update, orphan) per phase and writes it to `sync_run` without calling
  Medusa. This is the per-source graduation path: dry-run, inspect, then enable
  live per source behind the existing `mode: review|auto` trust gate.
- **Consistency becomes a query.** What `catalog.verify_consistency()` does with
  CSV set-diffs becomes a ledger invariant: a product whose `variation_key` has a
  null `medusa_id` is simply not eligible for Phase 5. The gate is structural,
  not a post-hoc cross-file check.

The existing trust ladder is preserved. `certify.py`, the health gate, and the
module gates (`gates/`) all still run during ingest. The engine refuses to push
entities from a source that did not pass its gates or is not `mode: auto`.

---

## 6. Image staging to live promotion

Per env there are two buckets: a staging bucket (for the `scraped/` and
`improved/` processing artifacts and the generated `{Key}.png` variant images)
and a live bucket (what Medusa serves). The `image` table tracks the crossing.

- Processing writes to staging and records `staging_key`, `state = staged`. The
  key is the content hash, so the same image is the same key and is never
  duplicated. This preserves the existing idempotency (manifest skip, content
  key dedup, exists backstop), now as a table instead of `_manifest.json`.
- Phase 4 of the engine promotes an image (copy staging to live, set `live_key`,
  `state = promoted`) only when a product that references it is about to go
  `synced`. Promotion is gated like every other phase, so nothing reaches the
  live bucket before the product that needs it is valid.
- The product row references the live url after promotion. A re-run with no image
  change is a no-op: the image is already `promoted`.

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
- Phase 6 pushes only rows where `qty != last_synced_qty`, then sets
  `last_synced_qty = qty`.
- The >30% delist guard becomes a count query on the ledger before Phase 6
  commits, refusing a mass delist from a partial scrape, exactly as today but as
  a query instead of a CSV diff.
- Discontinued products (supplier dropped them) set `qty = 0`, a reversible
  delist, recorded for audit.

---

## 8. The Medusa-side contract (the cross-repo ask)

This is the minimum the backend must provide. It is smaller than the sketch in
`MEDUSA_SYNC_PLAN.md`, which asked for server-side Key to id resolution for
combinations. That is not required.

Required:

1. **Store Key as `external_id`** on variations, attributes, combinations, and
   products.
2. **Upsert by `external_id` endpoints** for variations, combinations, and
   products. Upsert, never blind insert, so a re-run is idempotent.
3. **Return the full entity, including its id, in the upsert response.** This is
   the single property that kills the round-trip. A normal Admin API create or
   upsert already returns the entity with its id; confirm this holds. If it does,
   the reconcile engine reads the id from the response and writes it to the
   ledger, all in one process, with no human export download.
4. **Attribute read endpoint** (or the existing export) so Phase 1 can refresh
   attribute ids into the ledger. Attributes are a controlled vocabulary the
   backend owns; the pipeline reads them, it does not mint them.

Not required (drop from the plan):

- Server-side Key to id resolution for combinations. With client-side phase
  ordering and the ledger, the engine already holds every id by the time it
  builds combinations. This removes a custom transactional endpoint from the
  backend scope.

Optional, later:

- One atomic transactional sync endpoint, if a single all-or-nothing apply is
  wanted. The phase-ordered per-entity upserts are resumable and observable
  without it, so this is a refinement, not a prerequisite.

Re-seed handling (see section 9) needs no extra endpoint: the Keys survive a
re-seed, so re-resolution is automatic on the next sync.

---

## 9. Re-seed and fingerprint handling

When a Medusa environment is re-seeded, every internal id changes but every Key
survives. The ledger absorbs this without a manual re-pin.

- `RunContext.backend_id_fingerprint` is checked against the live backend at
  startup. On a mismatch, the engine does not abort. It marks all `medusa_id`
  columns in that env's ledger stale and sets the affected entities to
  `needs_resync`.
- The next reconcile pass re-resolves every id by Key through the normal upsert
  by `external_id` path. Keys carry the identity across the re-seed, so this is
  automatic.
- The `sync_run` row records the fingerprint change as an explicit event.

This is cleaner than today's abort and re-pin, and it is the same mechanism that
promotes dev to prod: point `RunContext` at the prod ledger and prod API, and
the first pass resolves prod ids by the same Keys.

---

## 10. Cross-ECS access, networking, secrets

The scraper, Medusa dev, and Medusa prod run on different ECS clusters. The
access surface is smaller than it appears, because the ledger is scraper-side
state that Medusa never reads.

- The only hop that crosses a cluster or account boundary is the reconcile engine
  to the Medusa Admin API over HTTPS, per env. The scraper ECS task needs egress
  to the dev Medusa API endpoint and the prod Medusa API endpoint, plus S3
  (staging and live, per env).
- Two API URLs and two admin tokens live in SSM SecureString, env-scoped, reusing
  the existing SSM and KMS secret pattern (`FAL_KEY`, scraper proxy). The
  `RunContext` reads the pair for its env.
- The ledger stays local to the scraper task. There is no cross-account database
  access. If the substrate is a server database, it sits in the scraper VPC and
  only the scraper task connects to it.

Net network story stays egress-only to a couple of HTTPS endpoints plus S3, which
matches the current least-privilege, egress-only posture.

---

## 11. Source-agnostic ingest (growth)

The `AdapterBase` boundary already makes any data source a first-class source.
The ledger and the reconcile engine never know whether canonical rows came from a
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

- If ingest and reconcile run serialized, one orchestrated run at a time per env,
  an embedded single-file database (SQLite on EFS, mounted by the scraper task)
  is safe, cheap, and zero-ops. It is the right substrate for the Phase 1
  prototype, which is single-process and write-through with no Medusa calls.
- The corpus will grow and parallel ingest is a likely optimization, and reconcile
  may overlap ingest. Concurrent scraper-side writers want a server database with
  row-level locking. A small RDS Postgres in the scraper VPC is the right target
  once reconcile and concurrent sources are live.

Recommendation: build behind a thin data-access layer so the substrate is one
swap. Prototype on SQLite on EFS (Phase 1 below). Graduate the same schema to RDS
Postgres when the engine goes live and concurrency appears. The schema and the
engine do not change across the swap; only the connection does.

Open decision for the user: graduate to Postgres at Phase 3 (when reconcile first
writes ids back, so the production store is already concurrent-safe), or run on
SQLite through the full migration and swap only when parallel ingest is actually
turned on. Default recommendation: swap at Phase 3.

---

## 13. Migration plan (phased, reversible, with a concrete cutover test)

Each phase is independently reversible and keeps the file flow as a fallback,
matching the "keep testing with the file flow" stance. The cutover criterion is
automatable, not a judgment call.

**Phase 1: write-through ledger (no Medusa calls).** Build the ledger schema and
a write-through data-access layer. The pipeline keeps emitting the current CSVs
and also upserts every entity into the per-env ledger. Seed the ledger once from
the current `from_medusa/<env>` exports.

Cutover test for Phase 1: render the ledger back out into the existing
`to_upload` CSV shapes (`1_variants_*`, `2_valid_combinations`, `3_products_*`,
`4_inventory_update`) and assert it is byte-identical, or fully diff-explained,
to what the CSV flow produces, across several dev runs. When that holds, the
ledger is proven to carry the same truth as the CSVs.

**Phase 2: reconcile engine in dry-run.** Build the engine. It reads the ledger,
computes the per-phase diff against Medusa, and writes the plan to `sync_run`
without calling Medusa. Compare the plan against the CSV flow until trusted.

**Phase 3: variations live.** Enable Phase 2 of the engine (upsert variations by
Key, write minted ids back to the ledger) for one dev source behind `mode: auto`.
The export download for variants is now gone. The CSV flow stays as fallback.
Recommended substrate swap to RDS Postgres here.

**Phase 4: combinations, images, products, inventory live.** Enable the remaining
phases, gated and ordered off ledger state. Retire the `to_upload` choreography
once the engine drives the full sync for the proven sources.

**Phase 5: retire `from_medusa` reads.** The pipeline reads attribute and
variation ids from the ledger (refreshed by Phase 1 of the engine), not from
downloaded export CSVs. The ledger is now the boundary. The CSV export of the
ledger can remain as a debug artifact.

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
- `SYNC_STEPS.md` as a human procedure (the engine phases replace it)
- `catalog.verify_consistency()` CSV set-diffs (now a ledger invariant)
- `_manifest.json` (now the `image` table)

---

## 15. Open decisions for the user

1. Substrate graduation point: swap SQLite to RDS Postgres at Phase 3
   (recommended), or run SQLite through the full migration and swap only when
   parallel ingest is enabled.
2. Confirm with the backend chat that the Medusa Admin API returns the entity id
   in the upsert response (section 8 item 3). If yes, the server-side Key
   resolution endpoint is dropped from scope.
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
