# Stone scrape to Medusa import pipeline

Implementation of `stone_pipeline_plan_v3_1.md`. Many heterogeneous scrapers fan
in through one canonical staging schema; one template-driven emitter fans out.
All expensive logic is written once against the canonical shape, so adding a
source is one thin adapter.

## Status: all milestones M0 to M12 implemented

The pipeline runs Stage 0 to Stage 10 front to back on all four sources and emits
55-column import CSVs (incl. Product Image 1…15) that validate against the real
upload and the three reference maps. Fully tested (87 tests). Two integration points are intentionally
left dry/unwired until real infrastructure exists: the S3 image backend (a local
staging backend runs in its place now) and the Medusa API sink (the CSV emitter
runs in its place now); both are one config/sink swap, not a redesign.

Run everything: `python -m stone_pipeline.run all` (per-source summaries +
artifacts). Run one source: `python -m stone_pipeline.run polonine`.

| milestone | what | state |
|---|---|---|
| M0 | repo skeleton, config block, canonical schema + Resolution/flag types, parquet IO, manifest, structured logging | done |
| M1 | reference loaders (attributes, variants, backbone, ports, units, synonyms, origin) + live-id fingerprint check + 6A trace | done |
| M2 | Stage 0 health gate + drift diagnosis (missing/renamed column, fill drop, row collapse, smoke test) | done |
| M3 | AdapterBase + declarative framework + polonine adapter + golden-fixture self-test | done |
| M4 | Stage 2 keys/dedup (surrogate minting), Stage 3 normalize (synonym/exact/fuzzy), shared matching engine projections | done |
| M5 | FieldResolver base, variation engine tiers 1 to 6 (exact, projection-exact, phonetic, fuzzy, overlap), Stage 5 tree reconciliation by membership, tree-gap queue | done |
| M6 | Stage 6 derivation: category, units/dimensions, bundle-size ladder, origin, ports, title/description/handle generation | done |
| M7 | Stage 7 images: content-addressed keys, branch slotting, placeholder block (S3 dry-run; download/upload wired when creds exist) | done (dry-run) |
| M8 | Stage 8 constants + Stage 9 validation + Stage 10 template-driven emit; emitted CSV validates against the real upload | done |
| M9 | overrides (top-priority strategy) + alias write-back (persisted) + idempotent re-ingest loop | done |
| M10 | splink (tier 7) + semantic (tier 8), config-gated, review-only; offline-testable embedder | done (gated) |
| M11 | marenostone, zucchi, varsha adapters with golden fixtures; generic-descriptor gap routing | done |
| M12 | run summary, multi-source isolated runner, row fingerprints, Medusa API sink interface | done |

### Image staging (your note: local now, S3 later)

`config.images.mode` selects the backend: `passthrough` (emit source URLs, the
current default), `local` (download + content-address into `state/image_staging/`),
or `s3` (upload the same content-addressed key to the staging bucket). The public
URL is computed identically for local and s3, so once the local staging dir is
synced to the bucket the emitted URLs already resolve. Switching local->s3 is one
config line. Downloads are bounded-concurrency with retry/backoff and isolated
failures (a dead image flags one row, never crashes the run).

### Medusa import

`io/medusa_client.py` defines one `ImportSink` interface with two implementations:
`CsvImportSink` (current, writes the template CSV) and `MedusaApiSink` (upserts on
the namespaced handle; dry-run until backend credentials exist). Swapping sinks
changes nothing upstream.

## Run it

```bash
python -m venv .venv
.venv/bin/pip install polars pyarrow rapidfuzz pydantic jellyfish pyyaml pytest
.venv/bin/python -m stone_pipeline.run polonine     # runs the spine, writes outputs/
.venv/bin/python -m pytest stone_pipeline/tests/    # 43 tests
```

Outputs land in `stone_pipeline/outputs/`: `canonical_<run>.parquet`,
`tree_gaps_<run>.csv`, `scrape_health_<run>.json`, `run_manifest_<run>.json`.

## What the spine proves on polonine (303 rows)

- Health OK, baseline learned.
- Attributes resolve: quality 303/303, type 303/303, colour 294, finish 288.
- Variation resolves 11 via exact/projection/phonetic tiers; the other 292
  products de-duplicate to ~72 distinct `missing_variation` tree gaps. This is
  correct: polonine's varieties are largely Brazilian stones not yet in the
  314-row slabs reference, so they route to manual assistance rather than being
  guessed (plan section 7A, 8.2). Result quality varies by source by design.
- Deterministic: a second run produces byte-identical canonical rows.

## Layout

```
config/      settings.py (the single config block), sources contracts, source_contracts.yaml
reference/   loaders.py, fingerprint.py, units.csv, synonyms/, stubs (origin/ports/placeholder/...)
core/        schema.py (canonical row + Resolution + flags), ids.py, manifest.py, logfmt.py
io/          staging.py (canonical parquet read/write)
matching/    projections.py, index.py, engine.py (shared by attribute + variation)
resolvers/   base.py (FieldResolver + Strategy)
adapters/    base.py, polonine.py, selftest.py, fixtures/<source>/
stages/      health, keys_dedupe, normalize, match_variation, reconcile_tree
run.py       the orchestrator
```

## Categories (slab / block / tile)

Categories live in one registry: the `CATEGORIES` tuple in `config/settings.py`.
A category is ACTIVE once its Medusa `pcat_id` is set there — no code change (see
`CATEGORY_GUIDE.md`). Slabs, blocks and **tiles are all active**. Each variety has
a different variation id per format; the category is the Key prefix
(`slab_`/`block_`/`tile_`).

Tiles MIRROR slabs (`mirror_of="slab"`): same varieties/finishes/colours, with
deterministic `tile_` Keys built by `stages/build_tile_backbone.py`. The single
combined export `from_medusa/variants_export.csv` carries every category's ids.
Until a category's variants are uploaded and re-exported, its scraped products gap
cleanly (the reference is empty) and resolve automatically once the export includes
that prefix.

## Reference data notes

- `catalog_source/` holds the supplied ground truth (attributes, slabs variants,
  backbone). `reference/` holds generated/stub data: `units.csv` and the
  `synonyms/` maps are seeded from the real scrape values; `ports.csv`,
  `origin_map.csv`, `placeholder_hashes.csv`, `standard_slab_area.csv`,
  `variants_blocks.csv` are stubs to be replaced with real data.
- Supply a real `ports.csv` into `catalog_source/` (the loader prefers it over
  the stub).
- The backbone holds ~11,645 varieties (names only); the slabs variants file
  holds 314 with ids. Variation matching is against the 314 with ids; the
  backbone is the validity authority. The large gap count on polonine reflects
  this lag, exactly as the plan anticipates.

## Binding invariants (from plan section 0)

No argparse, no em dashes, deterministic/idempotent, never guess a value into
output (below-floor goes to review or gap), provenance on every derived value,
fail loud and isolated, template is the schema authority, reference data and
aliases are living (write-back closes the loop).
```
