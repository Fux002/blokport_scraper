# do_do — deferred items

Running list of things to do later. Grouped by theme. Nothing here blocks the
current dev pipeline; these are for completeness, production, and polish.

## Images
- [ ] DEFAULT is now source links (no download): scrapers keep the source image
      URLs and the pipeline emits them directly (passthrough). Switch to
      downloading + S3 re-hosting only if/when source images need to be robust
      against removal (`ScraperBase.download_images_enabled = True` + `images.mode
      = "s3"`).
- [ ] Live-verify each migrated scraper (marenostone, zucchi) against its site
      once — they are structurally faithful but were built without network here.
- [ ] Catalog/variant images are generated in `image_pipeline/` (fal.ai img2img ->
      de-background -> `{Key}.png`, see `image_pipeline/IMAGE_FLOW.md`). Existing
      photos dropped into `images/inbox/` are matched to their `{Key}.png` by
      `python -m stone_pipeline.images` (stages/image_intake.py). Only product-backed
      varieties get generated (cost control); the catalog stamps the `Image` URL.
- [ ] Optionally collapse the prompt-string round-trip between `image_prompts.py`
      (builds a sentence) and `image_pipeline/genetate_images.py` (re-parses it) into
      structured fields — cosmetic, the generation works as-is.
- [ ] When real S3 staging is wired, set `SETTINGS.images.mode = "local"` then
      `"s3"` (currently `passthrough`); `io/storage.py` + `io/download.py` ready.

## Scrapers (10 migrated; PRIORITY = marenostone, varsha, polonine)
- [x] PRIORITY sources (the ones to actually scrape + import) all framework-ready:
      marenostone (Woo API, per-product format), varsha (SlabWare API, cookie auth),
      polonine (SlabWare API, no Playwright). The rest are reference examples.
- [x] Migrated + registered: marenostone, polonine, zucchi, develi, ferraz, fulei,
      temmer, tureks, brumagran, varsha. All format=slab except marenostone.
- [x] curl_cffi transport added to ScraperBase (`use_curl_cffi` flag + `impersonate`,
      lazy import, content->data kwarg). Enabled on polonine/ferraz/brumagran
      (Cloudflare-fronted SlabWare). Install once: `pip3 install curl_cffi`.
- [x] polonine now uses the SlabWare API directly (ObterListaBundles+DetalheBundle),
      mapping to the polonine adapter columns. scraper_slabware.py (Playwright) KEPT
      as the proven FALLBACK until the API path is verified live.
- [x] LIVE-VERIFIED + pipeline-tested the priority three (marenostone, polonine,
      varsha) on 2026-06-21. Importable: 74 / 134 / 372. curl_cffi installed; it
      bypasses Cloudflare (polonine 403 via httpx -> 200 via curl_cffi).
- [x] varsha does NOT need cookie auth — its SlabWare viewer is PUBLIC via
      curl_cffi. Rewrote scrapers/varsha.py from ferraz.py (public API +
      DetalheBundleNovo); browser_cookie3 no longer required for varsha.
- [ ] Live-verify the remaining example scrapers when those suppliers are wanted
      (develi, ferraz, fulei, temmer, tureks, brumagran, zucchi).
- [ ] WATERMARKS: varsha images are watermarked. Plan a cleanup step (download ->
      de-watermark -> re-host to S3) before they are customer-facing. Noted in varsha.py.
- [ ] Kept Cloudflare originals scraper_ferraz/brumagran/varshastones until verified.
- [ ] Optional consistency: extract a SlabwareScraper(ScraperBase) base from the
      shared polonine/varsha/ferraz/brumagran logic; make them thin tenant subclasses.
- [ ] slabware (Playwright, multi-tenant) only needed if the API path fails for a
      tenant. stonevip — empty; write when the site is ready.
- [ ] Declare each scraper's FORMAT explicitly so block/slab/tile is tagged at the
      source (not inferred): slabware sites (polonine/varsha/zucchi) -> `category =
      "slab"`; marenostone -> per-product (attr_format); others (develi/ferraz/
      tureks/temmer/brumagran/fulei/stonevip) -> set when migrated. The base then
      writes a `format` column the pipeline reads as an explicit tag (high conf).
- [ ] Real per-company `company_id` (and sales_channel_id) per source in
      `config/sources.yaml` — currently all share the dev id. Wired through to
      `STN Company Id` so each product lands under its owning company in Medusa.

## AWS / production (target architecture)
- [ ] Two-phase image flow: scrape -> push images (data/<source>/images/) +
      products.csv to an S3 staging bucket -> the product import script picks them
      up. Pieces ready: `io/storage.py` S3 backend (content-addressed keys),
      `io/download.py`, `SETTINGS.images.mode` (passthrough -> local -> s3).
- [ ] Set `images.mode = "s3"`, real bucket/region/prefix/profile in `SETTINGS.s3`
      and `SETTINGS.images.public_base`; emitted image URLs already match the keys.
- [ ] Run on AWS: scrapers + pipeline as scheduled jobs (Lambda/ECS/Batch),
      reading/writing S3 instead of local disk; secrets + ids from env (settings
      has `environment` + the backend-id fingerprint guard for prod vs dev).
- [ ] Direct Medusa API: swap the CSV `ImportSink` for `MedusaApiSink` so the
      import script consumes staged data via API (upsert on handle), not manual CSV.
- [x] Inventory-adjustment CSV columns confirmed: Variant Sku, Product Handle,
      Variant Title, Inventory Quantity, Reserved Quantity (importer reads only
      Variant Sku + Inventory Quantity). Implemented.
- [ ] Download the Medusa **product export** and save as `from_medusa/products_export.csv`
      so the pipeline can split new vs existing products (item 4). Absent = all new.

## Reference data to supply
- [ ] `reference/origin_map.csv` is a small stub — extend with real
      variety/pattern -> country/city/county. (`ports.csv` already real.)
- [ ] `reference/standard_slab_area.csv`, `placeholder_hashes.csv` — extend as
      real data accrues.

## Attribute vocabulary (colour / finish / type / quality)
- [ ] These are a CLOSED, manually-maintained set in
      `catalog_source/attributes.csv`. When the curation flags a
      genuinely new value (`recommended_action = new_value`), add it manually to
      that file (with the Medusa id) and to the backbone's allowed sets.
- [ ] Approve the synonym suggestions (`curation_attributes_*.csv`,
      `recommended_action = synonym`) into `reference/synonyms/*.csv` — pipeline
      side, no Medusa change.
- [ ] Backbone-update suggestions (`curation_backbone_updates_*.csv`): for each
      `verdict = likely_real`, add the missing colour/finish to that variety's
      backbone allowed set; for `verify_match`, check the match first.

## Medusa integration (currently manual file upload)
- [ ] Wire the direct Medusa API client (`io/medusa_client.py` `MedusaApiSink`,
      dry-run now) to replace manual CSV upload of products; upserts on handle.
- [ ] Later: have the curation/tree steps post to Medusa directly too, not just
      write files.

## Production stack migration
- [ ] Re-pin `SETTINGS.backend_id_fingerprint` against production ids; refresh all
      backend ids (company, sales channel, pcats, attribute ids) after re-seed.
- [ ] Per-source `company_id` / `sales_channel_id` for the real accounts
      (`config/sources.yaml`).

## Matching / data quality (backend side)
- [ ] Over-generic backend aliases cause a few wrong exact matches (e.g.
      "White Marble" listed as an alias of White Namibe). Prune those aliases.
- [ ] 741 duplicate variant NAMES in the reference (e.g. "Green" x5). The matcher
      disambiguates by type+colour, but cleaner names would help.
- [ ] Residual fuzzy/phonetic edge cases (e.g. Titanium Gold -> Black Cosmic).
      Optionally route low-confidence fuzzy to review-only for a conservative
      import, or grow aliases to convert them to exact.

## Optional / advanced
- [ ] splink (tier 7) and semantic (tier 8) variation tiers are built but gated
      off (`SETTINGS.matching.enable_splink` / `enable_semantic`). Heavy deps;
      may not install on Python 3.14. Enable only if needed.
- [ ] Incremental runs: only reprocess rows whose `row_fingerprint` changed.
