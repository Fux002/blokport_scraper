# Catalog & tree pipeline — dev/prod parity

The variant catalog and Medusa combinations are built so the **dev and prod catalogs
are structurally identical** — the only differences are the ids Medusa generates in
each environment and the S3 bucket in image URLs. See `RUNBOOK.md` for the full
workflow; this file is just the dev↔prod story.

## Two output sets, one core

The **core is shared**: the raw scrape (`data/`) and the hand-maintained
`catalog_source/` (backbones, ports — names, never Medusa ids) are env-independent.
Everything that carries Medusa ids is **per environment**, in sibling folders selected
by `BLOKPORT_ENV` (`development` default, `production`):

| folder | shared / per-env | holds |
|---|---|---|
| `data/`, `catalog_source/` | **shared** | the scrape + hand-maintained backbones (names) |
| `from_medusa/<env>/` | per-env | that env's downloads: `variants_export.csv`, `attributes.csv` |
| `to_upload/<env>/` | per-env | the upload set: variants, `2_valid_combinations.csv`, products |
| `review/<env>/`, `outputs/<env>/` | per-env | look-before-upload + internal staging |

So you **scrape once**, then run `catalog`/`tree` under each `BLOKPORT_ENV` to produce
both sets. Each resolves names→ids against its own `from_medusa/<env>/` reference, so
`to_upload/development/` carries dev ids and `to_upload/production/` carries prod ids.

## Deploying on AWS (development & production)

The whole pipeline runs on AWS with two setups: **development** and **production**.
Everything environment-specific is driven by env vars, so promotion is a config
change on the ECS task / Lambda / Batch job — never a code edit. The defaults
reproduce dev-staging, so an unset environment behaves exactly as before.

| Env var | Default (dev) | Purpose |
|---|---|---|
| `BLOKPORT_ENV` | `development` | `production` flips the S3 path segment to `prod/` and enables guards |
| `BLOKPORT_S3_BUCKET` | dev bucket | The bucket; **must be set in prod** (a prod run on the dev bucket warns) |
| `BLOKPORT_S3_REGION` | `eu-west-1` | AWS region |
| `BLOKPORT_AWS_PROFILE` | `default` | Credentials profile (use the task IAM role on AWS) |
| `BLOKPORT_S3_DRY_RUN` | `true` | `false` to actually upload to S3 |
| `BLOKPORT_IMAGE_MODE` | `passthrough` | `s3` to download → process → re-host product photos |
| `BLOKPORT_IMAGE_PROCESSING` | `false` | `true` to enhance/de-watermark before re-host |
| `BLOKPORT_KEEP_SCRAPED` | `false` | `true` to also keep the raw download in the sibling `scraped/` folder |
| `BLOKPORT_VARIANT_IMAGE_BASE` | dev variations URL | The variant `{Key}.png` base stamped into the catalog |

Product photos re-host under `{bucket}/{env}/products/` (content-addressed by source
+ sha256; the staging path you gave is `blokport-dev-staging-3e58a6/dev/products/`),
in two sibling folders so the upgraded set is cleanly separated:
- `{env}/products/improved/<src_site>/<sha256>.jpg` — the enhanced image **Medusa uses**.
- `{env}/products/scraped/<src_site>/<sha256>.jpg` — the raw download, same filename
  (only when `BLOKPORT_KEEP_SCRAPED=true`; downloads are otherwise in-memory and the
  untouched original still lives at the supplier URL).

Note this is distinct from the variant texture images at `{env}/variations/{Key}.png`.

Plan: wire to **development** first, test end to end, then set the production env
vars and re-run. The faithful enhancement runs anywhere (CPU); the de-watermark
torch stack (`requirements-imageproc.txt`) belongs in an **ECS/Batch container**
(GPU optional) — too heavy for vanilla Lambda.

## What is identical across dev and prod
- **Variant Key** (`{branch}_{type}_{name}_{uuid}`) — lives in the upload file,
  carried into both environments on import. Medusa never invents it.
- **Image name** `{Key}.png` — 1:1 with the Key; the same generated images upload
  to both buckets.
- **Backbone `key`** and the **tree node id** (`uuid5(path)`, derived from the
  hierarchy path) — same path → same node id in every environment. The tree joins
  backbone ↔ export by Key.
- The hierarchy: root > category > type > variation > finish > {color, quality}.

## What differs (per-environment only)
- Variant `Id` / tree `sourceId` (the ULID Medusa assigns on import).
- Attribute/category sourceIds — from each environment's
  `catalog_source/attributes.csv` mapping.
- The S3 bucket base in image URLs — the env var `BLOKPORT_VARIANT_IMAGE_BASE`
  (default = dev). This is the ONLY env-specific value in the produced files.

## The build, in order
```bash
python -m stone_pipeline.run all     # scrape every source -> per-source products
python -m stone_pipeline.catalog     # -> to_upload/ (1_variants_full.csv, products, ...)
python -m stone_pipeline.tree        # -> to_upload/2_valid_combinations.csv (after the export refresh)
```

## Promotion: dev → prod
1. Build the prod variant file: set the prod values and re-run the catalog —
   ```bash
   BLOKPORT_VARIANT_IMAGE_BASE="https://<prod-bucket>/.../variations/" \
       python -m stone_pipeline.catalog
   ```
   This stamps the prod image base into `to_upload/1_variants_full.csv`. (For the prod
   attribute/category ids, point `catalog_source/attributes.csv` at the
   prod mapping.)
2. Upload `to_upload/1_variants_full.csv` into prod Medusa.
   The Keys carry over — do NOT let prod mint new Keys. Download the prod export as
   `from_medusa/variants_export.csv`.
3. Copy the `{Key}.png` images to the prod bucket (same files as dev) and re-run
   `python -m stone_pipeline.tree`. Structure (node ids, names, Keys) is identical
   to dev; only the Medusa sourceIds differ.

## Inputs (source of truth, in `catalog_source/` + `from_medusa/`)
- `from_medusa/variants_export.csv` — the Medusa EXPORT (Key → sourceId + Id), download-only;
  the immutable "existing variants" the catalog reads. Refresh after each upload.
- `catalog_source/backbone_{slabs,blocks,tiles}.json`
  — per-category allowed combinations (each post carries its `key`). Tiles mirror
  slabs and are rebuilt by `python -m stone_pipeline.stages.build_tile_backbone`.
- `catalog_source/attributes.csv` — attribute/category name → Medusa
  sourceId (includes a `category,Tiles,<pcat>` row so the tree emits a Tiles node).

The UPLOAD files in `to_upload/` are PRODUCED by the catalog (never hand-edited);
it and the export above never alias.
