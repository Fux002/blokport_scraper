# Project: blokport_scraper (stone-pipeline)

Many heterogeneous stone-supplier scrapers fan in through one canonical staging schema;
one template-driven emitter fans out to Medusa import CSVs / a pull-based sync ledger.
Expensive logic is written once against the canonical shape, so adding a source is one thin adapter.

Alway stick to these coding principles: ~/.claude/CLAUDE.md

## Stack
- Python >=3.11 (CI + Docker run on 3.12). No argparse; config-driven, not interactive.
- Core libs: polars, pyarrow, pydantic v2, rapidfuzz, jellyfish, httpx, curl_cffi (Cloudflare TLS impersonation), boto3 (S3), pyyaml.
- Image stage: opencv-python-headless, numpy, pillow (core, CPU); optional torch + spandrel (Real-ESRGAN) for enhance; FAL FLUX Fill (fal-client, hosted API) for de-watermark.
- Persistence: SQLite — `config.db` (scraper control plane) and per-env sync ledger DB.
- Deploy target: AWS (Fargate scheduled task + on-demand Batch GPU), image in shared ECR, infra in Terraform.

## Environment and dependencies
- Virtualenv at `.venv/`. Install: `pip install -r stone_pipeline/requirements.txt`.
- Optional GPU/de-watermark extras: `pip install -r stone_pipeline/requirements-imageproc.txt` (needs the pytorch CPU index; see Dockerfile).
- No lockfile / package manager beyond pip + `pyproject.toml` (name `stone-pipeline`).
- Behavior is env-var driven. `BLOKPORT_ENV` (development|production) selects S3 bucket, key namespace, dry-run, image mode. Prod refuses to fall back to dev bucket / dev owner ids — it fails loud. Other keys: `BLOKPORT_S3_BUCKET`, `BLOKPORT_S3_REGION`, `BLOKPORT_SALES_CHANNEL_ID`, `BLOKPORT_COMPANY_ID`, `BLOKPORT_IMAGE_MODE`, `BLOKPORT_IMAGE_PROCESSING`, `BLOKPORT_SYNC_TOKEN`, `BLOKPORT_CONFIG_TOKEN`, `BLOKPORT_CONFIG_DB`.

## Commands
- Test: `pytest -q` (config in `pyproject.toml`: `testpaths=stone_pipeline/tests`, `pythonpath=["."]`).
- Certify sources (trust gate, runs in CI): `python -m stone_pipeline.certify all`
- Scrape: `python -m scrapers.run all` | `python -m scrapers.run <source> [<source> ...]`
- Pipeline (validate/match/derive/stage images): `python -m stone_pipeline.run all` | `python -m stone_pipeline.run <source>`
- Build catalog artifacts: `python -m stone_pipeline.catalog`
- Valid-combinations upload artifact: `python -m stone_pipeline.tree`
- Inventory-only refresh: `python -m stone_pipeline.inventory`
- One-command ordered build (self-verifying): `python -m stone_pipeline.build`
- Full from-scratch produce (fetch export → scrape → build, ledger write-through): `python -m stone_pipeline.produce`
- Housekeeping (drop superseded working data): `python -m stone_pipeline.clean`
- Variant-image management: `python -m stone_pipeline.images`
- Scraper config store: `python -m stone_pipeline.config.store seed|list`
- Config admin API (UI): `BLOKPORT_CONFIG_TOKEN=<t> python -m stone_pipeline.config.server`
- Sync ledger server: `BLOKPORT_SYNC_TOKEN=<t> python -m stone_pipeline.ledger.server` (routes `/sync/v1/*`)
- Container entrypoint (scrape→pipeline→catalog→upload to S3): `deploy/run_pipeline.sh` (RUN_MODE: pipeline|validate-dewatermark|reprocess)

### Docker
- `docker build --target core -t blokport-scraper:core .` — scrape + pipeline + CPU image enhance (scheduled Fargate task).
- `docker build --target imageproc ...` — core + CPU torch Real-ESRGAN enhance + FAL FLUX Fill de-watermark (local/CPU).
- `docker build --target gpu ...` — CUDA torch Real-ESRGAN enhance + FAL FLUX Fill de-watermark (AWS Batch). ESRGAN weights baked + pinned by SHA-256; de-watermark is the hosted FAL API (FAL_KEY, no baked model).

## Conventions
- **Binding invariants** (plan section 0): no argparse, no em dashes anywhere, deterministic + idempotent (same input → byte-identical output, safe to re-run), NEVER guess a value into output (below-floor → review queue or tree gap), provenance on every derived value, fail loud and isolated (one dead row/image flags that row, never crashes the run).
- **Single config block**: all paths, ids, thresholds live in `stone_pipeline/config/settings.py` (global) or `config/sources.yaml` / `config.db` (per source). Never inline tunables in stage/resolver code.
- **The template is the schema authority**: column names/order are read from the live Medusa export template at emit time, never hardcoded.
- **Source isolation invariant**: per-source setup lives ONLY in an adapter (`stone_pipeline/adapters/<source>.py`) + config. Never `if source == ...` in shared stages. A new site = fetcher (`scrapers/`) + adapter + config entry. Register scrapers in `scrapers/run.py` REGISTRY and adapters via auto-discovery. **Adding a source: follow `NEW_SOURCE_CHECKLIST.md` (the admission gate) + `stone_pipeline/adapters/ADAPTERS.md` (build steps); a source must clear every checklist gate before it is allowed to produce.**
- **Vendor product isolation**: the scraper only touches Medusa products in `scraper_sync_ref` (by external_id/SKU), never by company_id — never touch vendor-uploaded products.
- **One image per variant**: one `{Key}.png` per variant, replaced in place; never new names or `_2` suffixes.
- **Layout**: `scrapers/` (site fetchers), `stone_pipeline/` (config, core schema, io, matching, resolvers, adapters, stages, ledger, gates), `deploy/` (S3 fetch/upload + container ops), `image_pipeline/` (texture generation), `infra/` (Terraform), `catalog_source/` (supplied ground truth), `from_medusa/<env>/` (Medusa export inputs), `to_upload/<env>/` (emitted artifacts).
- **Generated / not source** (gitignored — do not edit or commit): `/outputs/`, `/state/`, `/data/`, `/to_upload/`, `/review/`, `/images/`, `/ledger/` (data), `config.db`, `*.parquet`, `from_medusa/**/variants_export.csv`. NOTE: the code packages `stone_pipeline/state/` and `stone_pipeline/ledger/` ARE source (only their runtime `*.csv/*.json/*.db` are ignored). `catalog_source/` and `from_medusa/**/attributes.csv` ARE committed source of truth.
- **Dev vs prod**: scrape once (shared `data/` + `catalog_source/` names, no ids); run catalog/tree per `BLOKPORT_ENV` because Medusa ids differ per environment. Promotion dev→prod is a config/env change, never a code edit.
- **Categories**: slab / block / tile registry is the `CATEGORIES` tuple in `settings.py`; a category is active once its Medusa `pcat_id` is set (no code change). Tiles mirror slabs (`tile_` Keys built deterministically).
- Extensive design docs live at repo root (`*.md`, e.g. HOW_THE_SCRAPER_WORKS, PIPELINE_OVERVIEW, SYNC_LEDGER_DESIGN, DEPLOY, SCRAPER_REQUIREMENTS). Consult before changing a subsystem.
