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
   independent `Key` (`{branch}_{slug(type)}_{slug(name)}_{uuid5("branch:name")}`,
   `core/ids.py` and `stages/curate.py`). The Key is stable for a fixed (branch,
   type, name) across runs and across dev and prod, and Medusa stores it as
   `external_id`. Because the type and name slugs are part of the Key, correcting
   either is intentionally a NEW identity (a re-key), handled explicitly as a
   retire plus create with a cascade, not a hash bump (section 5C). Everything
   else joins on Key.
2. **The ledger is the scraper-side system of record.** A small per-environment
   database holds every entity (variation, attribute, combination, product,
   inventory, image, gap), its last-known Medusa id, a content hash, and a sync
   state. The pipeline reads the ledger, never Medusa export CSVs.
3. **Medusa is the authority for id values, not a writer of the store.** The id
   originates in Medusa, but it travels back as data in the ack Medusa posts when
   it applies a pull, and the scraper-side sync service performs the write into
   the ledger. Medusa has no connection to the ledger and does not know it exists.
   See the writer model (section 3).
4. **Medusa pulls; the scraper never WRITES into it.** Stages produce canonical
   rows and upsert them into the ledger. A read-only sync service exposes the
   ledger's desired state, and Medusa pulls and applies at its own pace, acking
   the ids it mints back to the ledger. The scraper performs no write to Medusa.
   It makes at most two read-only calls: an optional attribute refresh (8.5) and
   the startup fingerprint check (sections 2, 9). "Never pushes" means never
   writes, not never reads. This keeps write pressure off the fragile system.
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
  category), `medusa_id` (nullable until synced), `state`
- existing values are refreshed from Medusa, but the pipeline also DISCOVERS new
  values (a new color, finish, type, or quality; surfaced today in
  `review/<env>/attributes_to_add.csv`). A discovered value starts `pending` with
  a null `medusa_id`, exactly like a variation, and any entity that references it
  is held until it is `synced` (section 8.4). Attributes are therefore a
  first-class synced entity, not a read-only mirror (fixes C3).

`combination`
- `combo_key` (PK) = hash(`category_pcat`, `type`, `variation_key`, color, finish,
  quality). Category and type are real pricing dimensions and MUST be in the key:
  this matches `2_valid_combinations.csv` (product_category_id, type_id,
  variation_id, finish_id, color_id, quality_id) built by `stages/tree_build.py`.
  Omitting them collapses distinct priceable rows (fixes C1).
- `category_pcat`, `type`, `variation_key` (FK), `color`, `finish`, `quality`
- `medusa_id` (nullable), `state`

`product`
- `sku` (PK) = `{source_code}-{surrogate_key}` uppercased
- `source`, `variation_key` (FK), `color`, `finish`, `quality`
- `dims` (weight, length, width, height), `image_keys`, `origin_country_code`
- `ports` (JSON array of Medusa port ids) = the SUPPLIER's shipping ports, sent to Medusa as
  `port_ids` = the port of origin. Sent RESOLVED so Medusa links them directly; Medusa MUST NOT
  derive ports from `origin_country_code` (the quarry country), which fanned every product out to
  all ports in its quarry country. `origin_country_code` remains the stone's origin display only.
- `payload_hash`, `medusa_id` (nullable), `state`, `last_synced`

`inventory`
- `sku` (PK, FK to product), `qty`, `last_synced_qty`, `updated_at`

`image`
- `sha256` (PK), `kind` in {scraped_photo, variant_texture}, `source_url`,
  `staging_key`, `live_key` (nullable)
- `state` in {staged, generating, promoted, failed}
- one row per unique image content. The product-to-photo link and the
  variation-to-texture link live on the owning row, never inferred from a
  filename. Readiness gates per lane: a variation on its `variant_texture`
  promoted, a product on its `scraped_photo` set promoted (fixes H1).

`gap`
- `kind` (missing_variation, missing_leaf_child, missing_attribute,
  image_generation_failed), `name`, `nearest_existing`, `nearest_score`,
  `example_url`, `state` in {open, resolved}
- the durable form of the per-run `tree_gaps.csv` and the review queues
  (`attributes_to_add.csv`, `variants_to_confirm.csv`), deduped across runs

`sync_run`
- `id`, `env`, `fingerprint`, `started`, `finished`, per-phase counts, status
- the audit ledger; one row per reconcile pass

State enum, shared by `variation`, `combination`, `product`:
`pending -> dirty -> syncing -> synced`, plus `needs_resync` (set on a fingerprint
change, section 9), `gap_held` (cannot sync; an open gap OR an unsynced referenced
attribute blocks it, sections 8.4, 5C), and `retiring` (an owned orphan, or the
old side of a re-key, being delisted, sections 5A, 5C). A `failed` ack returns an
entity to `dirty`; a persistently failing variant-texture generation escalates its
variation to `gap_held` with an `image_generation_failed` gap (fixes H2,
section 6A).

**`payload_hash` inputs (fixes M3).** The dirty-vs-synced decision rests on this
hash, so it covers exactly the fields in the entity's section 8.3 payload and only
those: variation `(branch, type, name, sorted(aliases), image_url, volume)`;
combination `(category_pcat, type, variation_key, color, finish, quality)`; product
`(variation_key, color, finish, quality, title, description, handle, weight,
length, width, height, origin_country_code, ordered(image_urls), company_id,
sales_channel_id, category, bundle_size, ports)`. Inventory uses no hash: the
`qty != last_synced_qty` compare drives it. A field Medusa applies must be in the
hash or a real change never re-syncs; a volatile field must be out of it or every
run churns.

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
    attributes    NEW values the pipeline discovered; ready immediately
    variations    ready once every referenced attribute is synced AND the
                  variant-texture image (if any) is promoted
    combinations  ready once their variation is synced AND their category/type/
                  color/finish/quality attributes are synced
    products      ready once variation + combination synced, referenced attributes
                  synced, and scraped photos promoted
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
- **Autonomy boundary (what flows without a human).** "Autonomous" means the
  proven, gate-passed, ungapped delta flows on its own. NEW varieties, NEW
  attribute values, and low-confidence matches go to the gap and review queues
  (`attributes_to_add.csv`, `variants_to_confirm.csv`) and wait for a human. That
  hold is the safety: an uncertain entity is never auto-listed, so it can never
  produce a faulty listing (fixes L1).
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
- Orphaning applies to variations and combinations too, not only products. An owned
  variation or combination with no remaining owned products (for example the old
  side of a re-key, section 5C) is retired through the same owned-only path, so a
  type or name correction never leaves a duplicate live variation behind.

Before any delist or orphan step, the engine asserts the target carries our
`external_id` or `company_id`. A target that fails the assertion is skipped and
logged, never acted on. This is a structural refusal, not a convention, so adding
sources or scaling the catalog can never put user-listed products at risk.

---

## 5B. Bootstrap full load, then deltas, and backward corrections

This is the explicit contract for "load the full set once, then only update,"
and for keeping the ledger and Medusa in sync without ever reloading everything.

**Bootstrap (one time, per env).** The first sync is a full load. The ledger is
seeded from the current Medusa state so every owned entity starts `synced` with its
real Medusa id: variations and combinations from the full variants and combinations
export, and existing owned PRODUCTS and INVENTORY by SKU from `products_export.csv`
(seeding products and inventory too is required, fixes M1, or the first delta would
re-create or orphan them). If a set is loaded fresh instead of seeded, the first
pull applies everything once: all entities are `pending`, the eligibility order
applies, and the acks capture the minted ids. Either way, after the bootstrap the
ledger mirrors Medusa for every owned entity.

**Steady state (every run after).** A scraper run does not reload anything. It
recomputes each entity's `payload_hash` and marks only the changed ones `dirty`
and the genuinely new ones `pending`. The sync service serves exactly that delta
for Medusa to pull. An unchanged entity stays `synced` with a matching hash and is
never served: no load, no churn. This is what keeps the database and Medusa in
sync without a full reload, and why steady-state sync is cheap. The full variant
and combination set is loaded once at bootstrap and only ever updated thereafter.

**Backward corrections come in two kinds (fixes C2).** Split by whether the
correction changes the Key:

- **Key-PRESERVING** (alias, color, finish, quality, dimensions, origin, image,
  title, description): the Key is unchanged, so the correction just recomputes
  `payload_hash`, flips the entity `synced -> dirty`, and the next pull re-applies
  it as an update on the same `external_id`. Same machinery as new data, no special
  flow, and the `sync_run` audit records it.
- **Key-CHANGING** (a corrected stone type or canonical name): because the type and
  name slugs are in the Key, the Key itself changes, so this is NOT a hash bump on
  the same `external_id`; it is a re-key (a new identity), handled by section 5C.
  Treating it as a plain correction would leave a duplicate variant and an orphaned
  old Key, i.e. faulty listings.

A correction that must remove an owned entity created in error is an explicit
retire action: mark the ledger entity `retiring`, and apply the owned-only orphan
action (stock 0, or a backend-confirmed delete only if that is ever brought into
scope). Removal is never an implicit delete triggered by mere absence from a
scrape, which protects against a partial scrape wiping good data (the same intent
as the >30% per-source delist guard, section 7).

---

## 5C. Re-keying: type and canonical-name corrections

Correcting a variety's stone type or its canonical name changes its Key (section 1,
C2), so the corrected variety is a NEW `external_id`, not an update of the old one.
The pipeline already does the producing half today (it re-mints the variety under
the new Key and flags the old one in `variants_to_delete`); the ledger must model
the full re-key as one cascade so Medusa never ends up with a duplicate variant or
products pointing at a retired variation.

A re-key is detected when an ingest produces a variation whose lineage (the same
source variety, now reclassified) maps to a new Key while the old Key is still
`synced`. The cascade, in order:

1. Create the new variation Key (`pending`), and re-key its combinations and
   products onto the new variation `external_id`. The combinations get new
   combo_keys, because the type is part of the combo_key (section 4, C1); product
   SKUs are unchanged (they key on source plus surrogate, not on the variety Key),
   so a product re-points, it is not recreated.
2. Serve the new variation, then its combinations, then its products, in the normal
   eligibility order, so Medusa creates the corrected identity and the products now
   reference it.
3. Only after the new side is `synced`, retire the old Key: its products have moved,
   so the old variation and its old combinations are owned orphans (section 5A) and
   are delisted (stock 0 / retire), never hard-deleted.

New-before-old ordering means there is never a window where a product points at
nothing: it is re-pointed to the live new variation before the old one is retired.
The `sync_run` audit records the re-key as a paired retire-plus-create so it is
traceable. Because SKUs are stable across a re-key, inventory and product identity
survive the reclassification.

---

## 6. Image staging to live promotion

Per env there are two buckets: a staging bucket (for the `scraped/` and
`improved/` processing artifacts and the generated `{Key}.png` variant images)
and a live bucket (what Medusa serves). The `image` table tracks the crossing.

- Processing writes to staging and records `staging_key`, `state = staged`. The
  key is the content hash, so the same image is the same key and is never
  duplicated. This preserves the existing idempotency (manifest skip, content
  key dedup, exists backstop), now as a table instead of `_manifest.json`.
- Two lanes, two gates (fixes H1). A `scraped_photo` is promoted just before the
  PRODUCT that references it is served `ready`; a `variant_texture` `{Key}.png` is
  promoted just before the VARIATION that carries it is served `ready`. Promotion
  is the readiness prerequisite for the matching owner, so the variation payload's
  `image_url` and the product payload's `image_urls[]` always point at bytes
  already in the live bucket, never a dead link.
- A re-run with no image change is a no-op: the image is already `promoted`.

Both lanes use the same staging-then-promote crossing, but they gate different
owners: the live bucket only holds a texture once its variation is ready and a
photo once its product is ready.

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

### When generation fails: escalate, never silently drop (fixes H2)

The fal.ai FLUX call can fail or hang (a missing timeout was found and fixed in
`image_pipeline/genetate_images.py`). Generation is bounded (retry plus timeout),
and the `image` row moves `staged -> generating -> promoted` on success. If
generation keeps failing, the image goes `failed` and its variation escalates to
`gap_held` with an `image_generation_failed` gap, visible in `GET /sync/status`
and the run summary. A stuck texture therefore surfaces as an explicit signal, and
its product is held with a reason, never left silently `pending` and absent from
the catalog forever. Decided: a product is HELD until its variant texture exists;
it never lists with scraped photos only. Every listing carries its uniform
generated texture, so a failed generation holds the product (with the
`image_generation_failed` gap as the signal) rather than degrade the catalog's
visual consistency.

---

## 7. Inventory lane

Inventory becomes a first-class entity with a fast lane that needs no product
re-import and no export download.

- An `inventory_only` ingest updates `inventory.qty` in the ledger.
- Inventory is served as `ready` only where `qty != last_synced_qty`; on the ack
  the scraper sets `last_synced_qty = qty`. Unchanged stock is never served, so a
  stock refresh moves only the deltas.
- The >30% delist guard becomes a count query the sync service applies before it
  serves inventory, refusing to expose a mass delist from a partial scrape. It is
  PER SOURCE (fixes L3), matching the current per-source guard in `run.py`, so one
  source's partial scrape cannot be masked or amplified by another source in the
  same env.
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
- `<type>` in `attributes`, `variations`, `combinations`, `products`, `inventory`.
- Returns a page of entities of that type that are ready to load now: owned, not
  held by an open gap, and with every prerequisite already `synced`. Eligibility
  is computed server-side (section 8.4), so the caller does not reason about
  dependencies.
- Each item carries its `Key` (or `SKU`), the full payload to load (section 8.3),
  and a `payload_hash` so Medusa can skip an entity it already applied at that
  hash.
- Pages order by an immutable column (`created_at`, then Key), so a concurrent ack
  flipping an entity to `synced` cannot make a page skip or duplicate an entity
  (fixes L2). The cursor is over that stable order, never over the mutable `state`.

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

The scraper reads Medusa in only two places, both read-only and never a write: the
optional attribute-list refresh (section 8.5) and the startup fingerprint check
(sections 2, 9). Everything else is the scraper exposing its ledger and Medusa
pulling from it.

### 8.3 Entity payloads (keyed by Key, attributes by canonical name)

Every payload is addressed by the stable `Key` (Medusa stores it as `external_id`)
and references other entities by Key or by canonical attribute name, never by a
Medusa id. Medusa resolves names and Keys to its own ids at load time, because
Medusa owns those ids. This is why the scraper does not need ids in advance and
why a re-seed needs no special handling.

- `attribute`: `category` (color, finish, quality, type, category) and the
  canonical `value`. Sent only for values the pipeline discovered that Medusa does
  not have yet; Medusa creates the value and acks its id (fixes C3). Existing
  values are not re-sent.
- `variation`: `external_id` = Key, `branch`, `type` (canonical name), `name`,
  `aliases[]`, `image_url` (a live-bucket texture url), `volume`. Medusa upserts by
  `external_id`, mints or matches the variation id, acks it.
- `combination`: `external_id` = combo_key, the canonical `category` and `type`
  names, `variation_external_id` = variation Key, and the canonical `color`,
  `finish`, `quality` names (fixes C1: category and type are combination
  dimensions, matching `2_valid_combinations.csv`). Medusa resolves the variation
  by Key and category/type/color/finish/quality by name, upserts, acks its id.
  Served only after the variation is `synced`.
- `product`: `external_id` = SKU (`{source_code}-{surrogate_key}`),
  `variation_external_id` = variation Key, canonical `color`/`finish`/`quality`
  names, `title`, `description`, `handle`, dims (`weight,length,width,height`),
  `origin_country_code`, `image_urls[]` (ordered live-bucket urls), `company_id`,
  `sales_channel_id`, `category` (canonical), `bundle_size`, ports. Served only
  after the variation and its combination are `synced`, its referenced attributes
  are `synced`, and its scraped photos are `promoted`.
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
| attribute | a discovered value not yet in Medusa; ready immediately |
| variation | every referenced attribute (its type) is `synced`, AND its variant-texture image (if any) is `promoted` (fixes C3, H1) |
| combination | its variation is `synced`, and its category/type/color/finish/quality attributes are `synced` |
| product | its variation and combination are `synced`, its color/finish/quality attributes are `synced`, and its scraped photos are `promoted` (fixes C3, H1) |
| inventory | its product is `synced` |

So Medusa can pull each type and apply, and it will never receive a combination
whose variation is not yet in Medusa, a product whose combination is missing, or
an entity that references an attribute Medusa has not created yet (fixes C3).
Acking a batch is what flips its entities to `synced`, which is what unlocks the
dependents on the next pull. The "complete one step before the next" guarantee
holds even though Medusa pulls at its own pace, because the gate is the data,
served only when safe. A held entity (an open gap) is never served until the gap
is resolved.

### 8.5 New attributes, idempotency, ownership, re-seed, failure

- **New attribute values (fixes C3).** When a payload would reference a canonical
  attribute value Medusa lacks, that value is served first as the `attribute` type
  and Medusa creates it and acks its id, OR it is held in the operator queue
  (`attributes_to_add.csv`) and its dependents stay `gap_held` until it exists.
  Either way no variation or product is ever applied against a missing attribute,
  so nothing lists with a null or unresolved attribute.
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
   (`attributes`, `variations`, `combinations`, `products`, `inventory`) pulls the
   ready batch from `GET /sync/<type>`, applies it, and acks the minted ids back to
   `POST /sync/ack`. `attributes` come FIRST: a discovered value is CREATED by its
   canonical (category, value) and its id acked, so no later variation, combination,
   or product references a value Medusa has not created yet (section 8.4, fixes C3).
   The other four types are upserted by `external_id`.
2. `external_id` storage and upsert-by-`external_id` for variations, combinations,
   products, and inventory; create-by-canonical-(category, value) for attributes.
   Resolve variation Keys and canonical attribute names to Medusa ids server-side at
   load (server-side resolution is required in this model, because Medusa is the one
   applying the load).
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

- `RunContext.backend_id_fingerprint` is checked at startup. This is a read-only
  call to Medusa (one of the two reads the scraper makes, principle 1.4, fixes M2),
  or it can be derived from the acked ids already in the ledger to avoid any live
  read. On a mismatch the scraper does not abort: it marks all `medusa_id` columns
  in that env's ledger stale and the affected entities `needs_resync`.
- The next pull re-resolves every id as Medusa applies each entity by Key and
  canonical name, and the acks repopulate the ledger. Keys carry the identity
  across the re-seed, so this is automatic.
- The `sync_run` row records the fingerprint change as an explicit event.

This is cleaner than today's abort and re-pin, and it is the same mechanism that
promotes dev to prod: point `RunContext` at the prod ledger and prod sync service,
and the first pull resolves prod ids by the same Keys. Note the distinction (C2): a
re-seed re-resolves the SAME Keys to new ids automatically, whereas a type or name
correction is a re-key (a NEW Key) handled by section 5C, not by this path.

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

- For the Phase 1 prototype (single-process, write-through, no Medusa calls) an
  embedded SQLite database is enough. But do NOT put the system-of-record SQLite
  file on EFS/NFS (fixes M4): SQLite's own docs warn that POSIX advisory locking
  over network filesystems is unreliable and fsync semantics differ, which can
  corrupt the file even single-process. Run Phase 1 SQLite on the task's LOCAL
  ephemeral disk with an S3 snapshot for durability, or go straight to RDS Postgres.
- The corpus will grow and parallel ingest is a likely optimization, and the sync
  service's ack writes can land while an ingest is running. Concurrent
  scraper-side writers want a server database with row-level locking. A small RDS
  Postgres in the scraper VPC is the right target once the sync service is live and
  serving a pulling Medusa.

Recommendation: build behind a thin data-access layer so the substrate is one
swap. Prototype on SQLite on local disk with an S3 snapshot (Phase 1 below), never
on EFS. Graduate the same schema to RDS Postgres when the sync service goes live
and concurrency appears. The schema and
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
9. DECIDED (hold): a product with no available variant texture stays held until the
   texture exists; it never lists with scraped photos only (section 6A). Kept here
   as a record; no longer open.

---

## 16. Review (correctness pass against the actual pipeline)

This section was added by a review of the design against the code that produces the
data it syncs (`stages/curate.py` `gen_key`, `stages/tree_build.py`,
`stages/product_state.py`, the `to_upload/<env>/*.csv` shapes, and the
gate/review flow). The goal is that the link works autonomously without faulty
listings, so each finding states what the doc says, what the code actually does,
why it matters, and the fix. Items C1 to C3 must be resolved before build; they
each produce wrong listings as written. The model below them is sound and is noted
at the end so the backend chat knows what to trust.

No em dashes here either, per the document convention.

**Resolution status (folded into the body).** All findings below are now resolved
in the design body; this section is kept as the audit trail and rationale.
- C1 (combination schema) -> sections 4 (`combination`), 8.3, 8.4.
- C2 (re-key) -> sections 1 (principle 1), 5B, the new 5C (re-key cascade), 5A, 9.
- C3 (attribute gating) -> sections 4 (`attribute`), 5 (flow), 8.2 (type enum), 8.3,
  8.4, 8.5, 8.6 (attributes pulled first).
- H1 (texture promotion) -> sections 4 (`image`), 6, 8.4.
- H2 (generation failure) -> sections 4 (state enum, `gap`), 6A, 15 decision 9.
- M1 (bootstrap seeding) -> section 5B. M2 (fingerprint read) -> sections 1.4, 8.2, 9.
  M3 (`payload_hash` inputs) -> section 4. M4 (SQLite on EFS) -> section 12.
- L1 (autonomy boundary) -> section 5. L2 (cursor) -> section 8.2. L3 (per-source
  delist guard) -> section 7.

### Critical (fix before building, each produces faulty listings)

**C1. The combination schema is wrong (missing category and type).**
The doc (sections 1, 4, 8.3) defines a combination as
`combo_key = hash(variation_key, color, finish, quality)` and the combination
payload as variation Key plus color/finish/quality. The actual combination the
pipeline emits (`to_upload/<env>/2_valid_combinations.csv`, built by
`stages/tree_build.py`) has columns:
`product_category_id, type_id, variation_id, finish_id, color_id, quality_id`.
So a combination is the tuple `(category, type, variation, finish, color,
quality)`. The doc omits `product_category_id` and `type_id`, which ARE
combination dimensions (pricing rules are keyed on the full tuple, including
category and type). As written the backend would build a collapsed combination
that merges distinct priceable rows, so prices and availability would be wrong.
Fix: combo_key hashes over `(category_pcat, type, variation_key, finish, color,
quality)`, and the combination payload (8.3) must carry the canonical category and
type names (Medusa resolves them), not just variation plus color/finish/quality.

**C2. The Key is NOT stable under a type or canonical-name correction.**
Principle 1 and section 5B state the Key is stable, Medusa stores it as
`external_id` and never changes it, and a backward correction recomputes
`payload_hash` and re-pushes "as an update through the same upsert-by-Key path."
But `gen_key` (curate.py) builds
`key = {branch}_{slug(type)}_{slug(name)}_{uuid5("branch:name")}`. The TYPE and the
NAME are part of the Key string, and the uuid5 is over `branch:name`. So correcting
a variety's type changes the Key (this happened this session: re-typing Agata from
Semi-Precious Stone to Agate turned
`slab_semi_precious_stone_agata_brown_<uuid>` into
`slab_agate_agata_brown_<uuid>`), and correcting a canonical name changes both the
name slug and the uuid, so it changes the Key too. A Key change is therefore NOT a
payload-hash update on the same external_id; it is a new external_id. If handled as
the doc's "backward correction," the corrected variety is created in Medusa under
the new Key while the OLD Key lingers, and that variety's combinations and products
still reference the old variation Key. Result: a duplicate variant, an orphaned old
Key, and products pointing at a retired variation, i.e. faulty listings.
Fix: split corrections into Key-PRESERVING (alias, color, finish, quality, dims,
origin, image, title, description) and Key-CHANGING (type, canonical name). A
Key-changing correction is a first-class re-key event: retire the old Key (the
owned orphan action, stock 0 / retire), re-key the variety's combinations and
products onto the new variation `external_id`, and only then serve the new Key. The
pipeline already does the retire half (`variants_to_delete` plus a re-mint under the
new Key); the ledger must model the re-key and its cascade explicitly, not as a hash
bump. Section 5B currently describes only the Key-preserving case.

**C3. New attribute values are not gated; "attributes already exist in Medusa" is
not always true.**
Sections 4 and 8.4 treat the `attribute` table as "refreshed from Medusa, not
minted," make variation readiness "always (attributes already exist in Medusa),"
and do not gate a product on its attributes existing. But the pipeline DISCOVERS new
attribute values (a new color, finish, type, or quality) and surfaces them in
`review/<env>/attributes_to_add.csv` for the operator to create in Medusa, then
adopts the id (`stages/decisions.adopt_attribute_ids`). So a variation or product
can reference a canonical name Medusa does not yet have. A payload referencing a
not-yet-created attribute name fails server-side name resolution at load, so the
entity lists with a null or unresolved attribute, silently and autonomously.
Fix: an attribute value that is not yet `synced` in the ledger (not yet created in
Medusa) is a gap that HOLDS its dependents. A variation or product is not `ready`
until every attribute it references (type, color, finish, quality, category) exists
in Medusa. Either add attribute creation to the pull contract (Medusa mints a new
value it lacks and acks its id) or hold the entity `gap_held` until the operator
creates it. The eligibility table in 8.4 must add "and every referenced attribute is
synced" to the variation and product rows.

### High

**H1. The variant-texture image is served before it is promoted.**
The variation payload (8.3) carries `image_url` as a LIVE-bucket url and variation
readiness (8.4) is "always," but section 6 promotes images "just before it serves
the PRODUCT." So a variation is handed a live url for a `{Key}.png` that has not been
copied to the live bucket yet, a dead link in Medusa. Section 6 conflates the two
image lanes. Fix: gate VARIATION readiness on its variant-texture image being
`promoted` (when it has one); gate PRODUCT readiness on its scraped photos being
promoted. Two lanes, two gates.

**H2. Image-generation failure has no escalation in the autonomous flow.**
Product readiness requires "images promoted," and gaps hold entities, but the
expensive variant-texture GENERATION step (the fal.ai FLUX call) is not modeled as a
failure mode. Generation can fail or hang (a missing timeout was found and fixed in
`image_pipeline/genetate_images.py` this session). A variety whose texture never
generates keeps its product `pending` forever, silently absent from the catalog,
with no gap and no signal. Fix: bound generation (retry plus timeout, now in code)
and model a persistently failing generation as an explicit held/escalated state that
shows in `GET /sync/status`, so a stuck image does not silently drop a product.
Decide whether a product may list with its scraped photos only (degraded but visible)
when the generated texture is unavailable.

### Medium

**M1. Product and inventory bootstrap seeding is under-specified.** Section 5B seeds
the bootstrap from "the existing full variants and combinations export." Existing
owned PRODUCTS and INVENTORY (seeded by SKU from `products_export.csv`) are not
stated, so on the first delta they could be re-created or orphaned. State that
products and inventory are also seeded by SKU at bootstrap, starting `synced` with
their real Medusa ids.

**M2. The cross-env fingerprint guard contradicts "the scraper never reads Medusa."**
Sections 1.4 and 8.5 say the only read from Medusa is the optional attribute refresh,
but the cross-env guard (2) and the re-seed check (9) compare against "the live
backend fingerprint" at startup, which requires reading Medusa's id set. Reconcile:
either acknowledge the fingerprint read as a second read-only Medusa call, or derive
the fingerprint from the acked ids already in the ledger so no live read is needed.

**M3. `payload_hash` inputs are unspecified.** The whole dirty/synced decision rests
on `payload_hash`, but the doc never lists the fields it covers. Omit a field Medusa
applies (the promoted image url, ports, bundle_size) and a real change is never
re-pushed; include a volatile field and every run churns. Specify the exact ordered
field set per entity that the hash covers (the same fields as the 8.3 payload, and
only those).

**M4. SQLite on EFS is a known reliability footgun.** Section 12 proposes "SQLite on
EFS, mounted by the scraper task" for Phase 1. SQLite's own documentation warns
against network filesystems: POSIX advisory locking over NFS/EFS is unreliable and
can corrupt the database, and fsync semantics differ. For a system of record this is
a real corruption risk even single-process. Fix: for Phase 1 run SQLite on the
task's LOCAL ephemeral disk with an S3 snapshot for durability, or go straight to RDS
Postgres. Do not put the system-of-record SQLite file on EFS.

### Low and clarity

**L1. Define the autonomy boundary explicitly.** The goal is autonomous operation,
and the design correctly autosyncs gate-passed, non-held entities, but NEW varieties,
NEW attribute values, and low-confidence matches go to `gap` / `needs_review`
(`attributes_to_add.csv`, `variants_to_confirm.csv`) and wait for a human. State
plainly that "autonomous" means the proven, gate-passed, ungapped delta flows without
a human, while uncertain entities are held for review by design. That hold is exactly
what prevents faulty listings: an uncertain entity is never auto-listed.

**L2. Pagination cursor stability.** `GET /sync/<type>?cursor=...` pages over a ledger
whose entities flip state mid-pull (an ack moves an entity to `synced`). Specify a
stable order (by immutable Key or `created_at`) and snapshot/`updated_at` semantics so
a concurrent ack cannot make a page skip or duplicate an entity.

**L3. State the delist guard scope.** Section 7's mass-delist guard should say it is
PER SOURCE, matching the current per-source 30 percent guard in `run.py`, so one
source's partial scrape cannot be masked or amplified by other sources in the same
env.

### What is sound (trust these)

- The ownership boundary (5A) and owned-only orphaning (stock 0, never a hard
  delete, asserted by `external_id` or `company_id`) is correct and matches the
  current >30 percent delist intent. This is the right protection for user-listed
  products.
- The pull-not-push model with a read-only sync API plus a scraper-side ack writer
  is a sound way to keep write pressure off Medusa while still getting every id back.
- Eligibility-as-barrier (serve only when prerequisites are `synced`) correctly
  replaces the manual ordering checklist with a data property, with the C3 caveat
  (add the attribute prerequisite).
- Re-seed by Key plus canonical name, re-resolved on the next pull, is correct and
  elegant, and reusing it for dev to prod promotion is a nice property, with the C2
  caveat (a Key change is a re-key, not a re-resolve).
- The deterministic Key as `external_id` is the right join for everything EXCEPT the
  Key-change case (C2).
- The generate-once / model-version image decision (6A) is sound and matches the
  existing `image_model.csv` audit.
