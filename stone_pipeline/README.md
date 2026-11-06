# stone_pipeline

Heterogeneous stone-supplier scrapers fan in through one canonical staging schema;
one template-driven emitter fans out to Medusa import CSVs and a pull-based sync
ledger. Expensive logic is written once against the canonical shape, so adding a
source is one thin adapter.

This package is the implementation. For how it works end to end, read the root
design docs:

- `HOW_THE_SCRAPER_WORKS.md`: plain-language overview
- `PIPELINE_OVERVIEW.md`: stages, module gates, the trust ladder
- `DEV_PROD_PIPELINE.md`: dev vs prod, product-image hosting
- `SYNC_LEDGER_DESIGN.md`: the pull-based Medusa sync (built and live)
- `STANDARDISE_PROCESS.md`: the standardised-photo process (GPU)
- `DEPLOY.md`: AWS deployment
- `CLAUDE.md`: conventions, the full command list, binding invariants

## Sources

The onboarded sources are the `REGISTRY` in `scrapers/run.py` (the one source of
truth): marenostone, polonine, zucchi, varsha, fuleistone, sentas. A new source is
a fetcher (`scrapers/`) plus an adapter (`stone_pipeline/adapters/`) plus a config
entry, and must clear `NEW_SOURCE_CHECKLIST.md` before it can produce. Per-source
logic lives only in its adapter, never an `if source ==` in a shared stage.

## Run it

```bash
pip install -r stone_pipeline/requirements.txt
python -m scrapers.run all          # fetch every source
python -m stone_pipeline.run all    # validate / match / derive / stage images
python -m stone_pipeline.catalog    # build catalog artifacts (per SCRAPER_ENV)
python -m stone_pipeline.produce    # full produce: fetch, scrape, build
pytest -q                           # the test suite
```

Behaviour is env-driven (`SCRAPER_ENV`, the `SCRAPER_*` keys); promotion dev to
prod is a config/env change, never a code edit. The full command list is in
`CLAUDE.md`.

## Categories

slab / block / tile are the `CATEGORIES` tuple in `config/settings.py`; a category
is active once its Medusa `pcat_id` is set there (no code change). Tiles mirror
slabs (`tile_` Keys built deterministically). See `CATEGORY_GUIDE.md`.
