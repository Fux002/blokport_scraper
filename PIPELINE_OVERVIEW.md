# Blokport scraper — full pipeline overview (handoff)

Scrapes natural-stone suppliers, normalises them against a controlled catalog, generates
and cleans imagery, and produces CSVs that load into a **Medusa** webshop. Runs on **AWS
Fargate**, config-driven for **dev → prod**. There are **three lanes**: the **data lane**
(scrape→CSVs), the **scraped-photo lane** (clean real product photos), and the **variant-
image lane** (AI-generate a uniform texture per variety). They converge at the Medusa import.

---

## 0. Inputs (what the pipeline reads)

- **`data/<source>/<ts>/products.csv`** — raw scrape output (per source).
- **`from_medusa/<env>/`** — Medusa exports (READ-ONLY): `variants_export.csv` (Key→Medusa Id),
  `attributes.csv` (attribute/category name→Medusa sourceId), `products_export.csv` (existing SKUs, for deltas).
- **`catalog_source/`** — hand-maintained, shared across envs: `backbone_{slabs,blocks,tiles}.json`
  (allowed variety×color×finish×quality sets), `backbone_additions/`, `ports.csv`, `origin_map.csv`, `synonyms.csv`.
- **`stone_pipeline/config/sources.yaml`** — per-source config (`adapter`, `source_code`, `mode: review|auto`,
  `watermarked`, `origin_default`, ports, bundle size).

**Identity key:** every variety has a deterministic **`Key`** = `{branch}_{type}_{name}_{uuid5(path)}`.
Keys are stable across runs/envs; Medusa mints internal Ids but never changes the Key. Everything joins on Key.

---

## 1. Data lane — scrape → Medusa-ready CSVs

### 1a. Scrape (`scrapers/`)
`ScraperBase` (`scrapers/base.py`): a source subclass implements `list_products()` + `parse_product()`.
Base handles HTTP (rotating UA, retries, backoff, **SSRF guard**, optional **curl_cffi** Chrome-TLS to beat
Cloudflare via `BLOKPORT_SCRAPER_PROXY`), and writes `data/<source>/<ts>/products.csv` with the source's
columns + `format, image_count, image_urls, image_filenames_local, scrape_timestamp, raw_json`.
(e.g. `polonine.py` hits the SlabWare API directly.)

### 1b. Pipeline (`python -m stone_pipeline.run all`) — stages IN ORDER (`stone_pipeline/run.py`)
1. **health** — schema-drift gate vs a column contract baseline → OK/DEGRADED/FAILED (FAILED aborts).
2. **adapt** (`adapters/`) — map source columns → `CanonicalRow` (`src_*` identity, `raw_*` values). No logic, just mapping.
   - **→ ingest gate** (`gates/`, contract `gates.definitions.INGEST`): the post-scrape check — reports per-row which raw fields each downstream module needs are missing (variety name, a dimensions source, image source). Report-only (never rejects); a field absent across most of the batch escalates OK→DEGRADED→**FAILED** (a systemically incomplete scrape, caught here not at upload).
3. **keys_dedupe** — assign `surrogate_key`; exact-dedup on SKU; flag near-dups.
4. **format_resolve** — Block / Slab / Tile (trust ladder: override→tag→name→inference→Slab fallback). Routes the branch.
5. **normalize** — resolve `type/color/finish/quality` against closed vocab (synonym→exact→fuzzy→unresolved).
6. **match_variation** — match the variety name to the branch's variant index (exact→fuzzy→phonetic→review→gap); auto-accepts write an **alias writeback** for next run.
7. **reconcile_tree** — enforce validity: color/finish/quality must be in the matched variety's allowed sets; fill gaps from the variety.
   - **→ clean gate** (contract `gates.definitions.clean_contract(ref)`): each resolved attribute name must be the **canonical spelling** for its id (the standing safety net for the casing-leak bug class — flags, never rejects), and reports how many rows still carry an unresolved tree gap (validate hard-rejects those).
8. **derive** — category, bundle size, dimensions, origin, title, description, handle/slug (dependency-ordered resolvers).
   - **origin** (`derive_origin`): scrape country → `origin_map` (per-variety) → **supplier-country fallback** (`origin_default`, low-confidence, flagged `origin_supplier_default` for review). Medusa requires an origin for its pricing-rule lookup, so a blank breaks the import; the fallback fills it while queuing the row to expand `origin_map`.
9. **images = Stage 7** (`stages/images.py`) — download→hash→dedupe→block placeholders→store→slot (Blocks: Front/Right/Back/Left; Slabs: Image 1–5). See lane §2. Skipped for inventory-only runs.
10. **constants** — company_id, sales_channel_id, visibility, discountable.
11. **product_state** — SKU (`<source_code>-<surrogate_key>`) vs `products_export.csv` → `new` / `existing`(+inventory delta). **>30% delist guard** refuses mass-delist from a partial scrape.
    - **→ process gate** (contract `gates.definitions.PROCESS`): enforces the Medusa-importable output contract — `origin_country_code` required (Medusa pricing rule). A row still missing it is hard-rejected with a precise reason (`process_missing_origin_country_code`) rather than emitted broken; reject flows through `validate`'s routing.
12. **validate** — hard rejects (null ids, tree gaps, no images) vs soft flags → emit / review / rejects (per `emit_on_review`).
13. **emit** — write the 55-column Medusa product rows (incl. **Product Image 1…15**, filled per product up to `SETTINGS.images.product_image_slots`) + review/rejects CSVs + `canonical.parquet` (diagnostic). Alias writeback + gap queue flushed at end.

### 1c. Catalog + tree (`stone_pipeline.catalog`, `tree.py`) — consolidate all sources
`python -m stone_pipeline.build` runs **scrape → run all → catalog → tree → consistency gate** in order.

### 1d. Output CSVs → `to_upload/<env>/`
| File | What it is |
|---|---|
| `1_variants_update.csv` | **delta**: new varieties + alias updates this run (import to create/update variants) |
| `1_variants_full.csv` | complete variant set (existing export ∪ updates ∪ tile mirror); `Image` column stamped |
| `2_valid_combinations.csv` (+`_update`) | every valid `(category, type, variation, finish, color, quality)` tuple per variety |
| `3_products_all.csv` / `3_products_<source>.csv` | 55-col Medusa product rows (handle, title, dims, SKU, color/finish/quality/variation ids, up to 15 image URLs) |
| `3_products_{new,existing}.csv` | split by `product_status` |
| `4_inventory_update.csv` | stock-only deltas (Variant SKU + Inventory Quantity) — no product re-import |
| `products_discontinued.csv` | SKUs the supplier dropped (set to stock 0; audit) |
| `SYNC_STEPS.md` | generated checklist of the exact ordered upload actions |

**Data model:** **Variation** (canonical variety, stable Key, Medusa-minted Id) → **Valid Combinations**
(pre-computed relational table of all priceable color×finish×quality per variety) → **Product** (a supplier's
concrete offering, references variation_id + attribute ids + dims, keyed by SKU). Combinations/products need the
variation **Id**, which only exists after variants are imported → the round-trip in §4.

---

## 2. Scraped-photo lane (`stone_pipeline/io/image_processing.py`) — Stage 7

Real photos of the actual slabs, shot in storage units. **Faithful, classical (OpenCV) — no invented detail.**
Enabled by `BLOKPORT_IMAGE_MODE=s3` + `BLOKPORT_IMAGE_PROCESSING=true`. Per image:
1. **De-watermark** (flagged sources, e.g. varsha): locate the fixed logo by its **pink/magenta hue** (a colour
   natural stone never has) + a fixed-central fallback, then **LaMa inpaint**. (Replaced unreliable Florence-2.)
2. **Enhance**: gray-world white-balance (±15% clamp), CLAHE on L-channel, light NLM denoise, unsharp mask.
3. **Resize**: **downscale-only**, cap long edge 1600, q85 JPEG.

**S3 layout** (`blokport-dev-staging-3e58a6`): `dev/products/improved/<src>/<sha256>.jpg` (final, linked by products),
`dev/products/scraped/<src>/<sha256>.jpg` (raw archive), `dev/products/_manifest.json` (`source_url→improved_url`).
**Key = SHA-256 of original bytes** → same image = same key = overwrite-in-place, never duplicates.
**Idempotency (3 layers):** manifest skips known URLs → content-key dedup → `exists()` backstop. Only new products process.

## 3. Variant-image lane (`image_pipeline/`) — generate a uniform texture per variety

Separate, **gated** lane (needs `FAL_KEY`; weights/`ben2` not in the deployed core image). Generative, not faithful.
1. `python -m stone_pipeline.stages.image_prompts` → `prompts_to_generate.json` (one prompt per **product-backed**
   variant Key, base image per category, directive: "no shadows/glare/text/watermark, flat straight-on view").
2. `image_pipeline/genetate_images.py` → **FLUX.2 [max] on fal.ai** (image-to-image edit, fixed seed for uniform
   geometry) → `images/{Key}.png` (~$0.10/img).
3. `image_pipeline/rb_images.py` → **BEN2** background removal → `to_upload/{Key}.png` (RGBA).
4. `aws s3 cp ... s3://<bucket>/<env>/variations/{Key}.png`. The variant CSV `Image` column already points here.

---

## 4. Medusa import/export round-trip (the human+machine cycle)

Because products/combinations need the variation **Id** that Medusa mints on import:
1. **Build** → `1_variants_update.csv` (+ products/combos that resolve).
2. **Import `1_variants_update.csv`** to Medusa → Medusa upserts by Key, assigns Ids to new variants.
3. **Export** `variants_export.csv` from Medusa (now has the new Ids) → save to `from_medusa/<env>/`.
4. **Re-build** → products + `2_valid_combinations` regenerate against the fresh Ids; **consistency gate** verifies
   every product/combo `variation_id` exists in the export (fails if stale).
5. **Import combinations, THEN products** (combos must exist first). Daily: only the small deltas + `3_products_all`.
6. Repeat until `SYNC_STEPS.md` shows nothing new. **Inventory-only**: just push `4_inventory_update.csv`.

**Freeze rule:** don't introduce new variants between steps 2–3, or their Id is missing when combinations build
(they wait in the gap queue for the next loop). **Planned automation** (`MEDUSA_SYNC_PLAN.md`): one atomic
**upsert-by-Key** Admin-API push (Medusa stores Key as `external_id`, resolves Key→Id server-side) kills the round-trip.

---

## 5. Dev → prod
Same Keys + same `{Key}.png` images in both envs; **only Medusa Ids and the S3 bucket base differ**. Promote by
re-running catalog with `BLOKPORT_VARIANT_IMAGE_BASE=<prod-bucket>/.../variations/`, importing into prod Medusa
(Keys carry over), downloading the prod export, copying the same images to the prod bucket.

## 6. Deployment & ops
- **Fargate** scheduled task in the shared Medusa dev cluster; **ECR** `blokport-scraper` (`:core`, `:imageproc`).
- **Terraform** `infra/`: egress-only SG, private subnets, OIDC deploy role, SSM SecureString secrets (`FAL_KEY`,
  `BLOKPORT_SCRAPER_PROXY`), encrypted+locked state. `cd infra && terraform apply`.
- **CI/CD** `.github/workflows/`: `ci.yml` (pytest + certify), `deploy.yml` (OIDC build+push; `build_imageproc=true`).
- **Entry** `deploy/run_pipeline.sh` via **`RUN_MODE`**: `pipeline` (default) · `validate-dewatermark` · `reprocess`.
- **Trust/state**: `certify.py` (config/adapter/selftest/contract gate, CI), `state/` alias writeback (learning loop),
  `mode: review|auto` per source, the consistency gate, the >30% delist guard.
- **Module gates** (`gates/`) — per-boundary contracts (ingest/clean/process; images/upgrade planned) that run inside
  every source's pipeline. `run all` prints a **per-source gate overview** (which module each source falls over in) and,
  as part of the trust ladder, **refuses to promote a `mode: auto` source that did not pass its gates** (exit non-zero;
  keep it in review or fix it). This is the onboarding funnel for a new source: certify → health → ingest → clean → process.

## 7. Current state (sources)
varsha ✅ (de-watermarked, verified) · polonine ✅ · marenostone ✅ · zucchi ❌ (scrape gate — scraper chat).
Product CSV: 798/870 linked, 0 broken; 72 imageless = supplier never photographed them.

## 8. Open / handoffs
1. **Publish current CSVs to S3** `to_upload/` (S3 copies stale) — scraper chat.
2. **Medusa re-import** products from `improved/`-linked CSV → user applies the **Blokport watermark** in Medusa
   media (`d1xcekdxyhabdd.cloudfront.net`, a SEPARATE system from our `improved/`).
3. **Medusa sync automation** (upsert-by-Key) — designed in `MEDUSA_SYNC_PLAN.md`, not built.
4. zucchi gate, variant sync (lot-number aliasing), passthrough unit test (non-hermetic) — scraper chat.

## 9. Security (reviewed + hardened, no critical/high)
SSRF guard (`io/ssrf.py`), setuptools≥78.1.1, all GitHub Actions SHA-pinned, LaMa weights checksum-baked, Florence/
`trust_remote_code` removed, `ben2` pinned/flagged. Secrets via SSM/KMS, least-privilege IAM, egress-only network.

## 10. Commands & gotchas
```bash
python -m stone_pipeline.build                 # scrape→catalog→tree→gate
SRC=<src> WATERMARKED=<bool> python -m deploy.reprocess_source   # re-clean a source's images in place
python -m deploy.cleanup_images [--apply]      # prune images not in the catalog (S3 hygiene; dry-run default)
aws ecs run-task ... --no-cli-pager            # run-task can HANG without --no-cli-pager
```
- `dewatermarked=True` ≠ removed — verify by eye. · Settings changes don't retroactively reprocess (do it explicitly).
- `scraped/` originals let you reprocess without re-downloading. · The two image lanes (variations/ vs products/) are distinct.
- Variant set must be frozen mid-sync; combos before products; Keys are the cross-env identity.

**Key docs:** `RUNBOOK.md` (the sync loop), `DEV_PROD_PIPELINE.md`, `MEDUSA_SYNC_PLAN.md`, `DEPLOY.md`,
`image_pipeline/IMAGE_FLOW.md`, `stone_pipeline_plan_v3_1.md` (full design), `stone_pipeline/README.md`.
