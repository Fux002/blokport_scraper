# Runbook — how the scraper keeps Medusa in sync and imports products

Read this if you forget how the whole thing works. It explains the flow, where
the files live, and what you do after a scrape.

## The one-line version

The scraper turns messy supplier scrapes into a Medusa product import. Before the
products can be imported, Medusa's catalog (variants, aliases, the allowed-combo
tree) must contain everything the products reference. The pipeline produces every
file you upload in ONE folder — `to_upload/` — with a checklist (`SYNC_STEPS.md`).

## Run it

ONE self-verifying command builds the whole upload set in the correct order:

```bash
python -m stone_pipeline.build            # run all -> catalog -> combinations -> consistency gate
python -m stone_pipeline.build --verify   # re-run ONLY the consistency gate on the existing files
# then follow to_upload/<env>/SYNC_STEPS.md to upload

# <env> is development by default. For the production set, prefix the command:
#   BLOKPORT_ENV=production python -m stone_pipeline.build
# The scrape (data/) is shared across envs, so the second env just re-runs build.
```

`build` runs the three stages in dependency order and ENDS on a consistency gate that fails
(non-zero exit) if any product or combination references a variation id that is not in the current
export — so a partial or out-of-order run can never ship a stale upload set. You never have to
eyeball it. The stages still exist individually for debugging (`run all`, `catalog`, `tree`), but
`catalog` already builds the combinations, so **you never run `tree` by hand**.

> Why this exists: the pipeline used to be three commands sequenced by hand with no freshness check.
> Forgetting `tree` (or running it before the export was refreshed) silently shipped STALE
> combinations. `build` + the gate make that impossible.

## Where the files live — four top-level folders

```
SHARED across environments (the "core"):
  data/              <- the raw scrape (env-independent)
  catalog_source/    <- the data you MAINTAIN by hand (backbones; names, not Medusa ids)
PER ENVIRONMENT — development/ and production/, selected by BLOKPORT_ENV
(the Medusa pcat / attribute / variation ids differ per env, so each has its own set):
  from_medusa/<env>/ <- SAVE that env's Medusa downloads here (variants_export, attributes); READ-only
  to_upload/<env>/   <- PRODUCED by the pipeline; UPLOAD these to that env's Medusa (numbered order)
  review/<env>/      <- look before uploading; never uploaded
```
You scrape ONCE (shared), then build the catalog/combinations per env:
`BLOKPORT_ENV=development` (default) and `BLOKPORT_ENV=production` each read/write their own
`from_medusa/<env>/` + `to_upload/<env>/`. Everything below shows paths relative to one `<env>/`.

### `to_upload/`  — everything you push to Medusa, in number order
```
1_variants_full.csv      complete variant list (first / full import)
1_variants_update.csv    only the new + alias updates (incremental; Medusa upserts on Key)
2_valid_combinations.csv          one row per valid combination — FULL set (one-time bulk load / resync)
2_valid_combinations_update.csv   ONLY combinations new since the last build — the DAILY upload (delta)
2_valid_combinations_products_only.csv  TESTING one-off (combos for the product sheet only; drops off in prod)
3_products_all.csv       every source's products combined
3_products_<source>.csv  per source (set a company per supplier in config/sources.yaml)
SYNC_STEPS.md            the checklist, with counts
```

### `from_medusa/`  — what you download from Medusa
```
variants_export.csv      the variant export (Id + Key); the catalog reads it as the "existing" set
products_export.csv      (optional) the product export, to split new vs existing products
attributes.csv           colour / finish / type / quality / category  ->  Medusa id   (your "colour/type" file)
```

### `catalog_source/`  — maintained by hand
```
backbone_slabs.json      allowed combos per category (append new varieties here)
backbone_blocks.json + backbone_tiles.json
backbone_additions/      per run: the new varieties + value changes to apply to the backbones
ports.csv + missing_variants.csv + image_model.csv
```

### `review/`  — decide before uploading (never uploaded)
```
variants_update_triage.csv  alias-vs-new, with nearest match + score
alias_candidates.csv + attribute_synonyms.csv
tree_uncovered_variations.csv  variations with no resolvable type (fill `assign_type`, allocated next run)
images_to_generate.csv      the {Key}.png to generate (see image_pipeline/)
```

The catalog is a **pure function**: it READS the immutable `from_medusa/variants_export.csv`
("existing") and WRITES `to_upload/1_variants_*.csv` — the two never alias, so re-running
gives byte-identical files. No file is hand-edited; there is no "reset" step.

(The raw per-source run output — canonical data, diagnostics, the un-gathered product
CSV — stays under `stone_pipeline/outputs/<source>_*/`; you never upload from there.)

## The flow (the round-trip loop)

The catalog is a **round-trip**: a product can't get its variation id until the variant it
references exists in Medusa and has been re-exported. So each cycle is one `build`, an upload, a
re-export, and another `build`. You repeat until `SYNC_STEPS.md` shows nothing new.

```
LOOP:

  1. python -m stone_pipeline.build
       -> to_upload/<env>/ : 1_variants_update.csv, 3_products_*, 2_valid_combinations*,
          SYNC_STEPS.md   (+ catalog_source/backbone_additions/ for the backbones)

  2. Upload  to_upload/<env>/1_variants_update.csv  to Medusa
       (the full set is already there; Medusa upserts on Key) -> Medusa assigns ids to new variants

  3. Download the variant export, SAVE AS  from_medusa/<env>/variants_export.csv
       !! EXACT name `variants_export.csv` (Medusa exports `variations_export.csv` — rename it).
          There is ONE canonical input name; no fallback.

  4. python -m stone_pipeline.build      (again — now the new ids resolve)
       -> products that were waiting now emit; combinations are rebuilt against the new export;
          the consistency gate verifies every product + combination matches the export

  5. Upload combinations, THEN products (combinations must exist before the products that use them):
       - first time / resync :  2_valid_combinations.csv          (FULL — large, once)
       - every day after     :  2_valid_combinations_update.csv   (DELTA — only new combos;
                                                                   EMPTY when nothing changed)
       - then                :  3_products_all.csv   (or 3_products_<source>.csv per source)

  (Images are a separate lane, any time — see "Images & S3" below.)
```

**Why the re-run matters:** a product's variation id comes from the export, so after the export
changes the products must be regenerated against it BEFORE the combinations — `build` does this in
order, then the gate refuses to ship if either is stale.

**Brand-new BRANCH = two loops:** if a scrape needs a variety in a category it doesn't yet have
(e.g. a block product for a variety that only existed as a slab/tile), loop 1 MINTS the block
variety and loop 2 attaches the scraped spelling as its alias so the block product finally resolves.
A new block variety therefore takes two upload cycles — expected, not a bug.

The combinations file is one row per valid combination (`product_category_id,type_id,variation_id,
finish_id,color_id,quality_id`) for Medusa's relational `valid_combination` table — flat rows, no
size cap. EVERY imported variation is made priceable, so it only changes when a brand-new VARIANT
appears (not when an existing variation is first sold). Each variation gets its category's
PRODUCT-USED finish set (the finishes that category's products actually carry; tiles mirror slabs) x
the colour(s) we know x quality. The few variations whose type can't be resolved land in
`review/<env>/tree_uncovered_variations.csv` (covered once a scrape classifies them).

## Daily production loop (incremental)

After the initial full load, the daily run is cheap because everything ships as a DELTA:

```
python -m stone_pipeline.build         # scrape + rebuild + gate
  -> 1_variants_update.csv             upload (new variants only; upserts on Key)
  -> re-export to variants_export.csv
python -m stone_pipeline.build         # ids resolve; gate verifies
  -> 2_valid_combinations_update.csv   upload (ONLY new combinations — usually tiny or EMPTY)
  -> 3_products_all.csv                upload (products)
```

You never re-upload the ~2M-row full combinations file day to day — only the delta of new
combinations since the last build (the previous build is the baseline; `delta = current - previous`).
If a daily delta is ever skipped, upload the full `2_valid_combinations.csv` once to resync.

## Consistency gate (self-verifying — no manual checking)

Every `build` ends on `catalog.verify_consistency()`, a deterministic set-containment check (no
heuristics, no sampling):

- every combination's `variation_id` must exist in the current `variants_export.csv`;
- every product's `STN Variation Id` must exist in it;
- every product variation must have at least one valid combination.

If anything fails, the build exits non-zero and names the problem (e.g. "N combination variation ids
are NOT in the current export — stale combinations"). Re-run it any time with
`python -m stone_pipeline.build --verify`. This is what guarantees the upload set is internally
consistent — you do not have to verify it by hand.

## Production (development -> production)

The whole pipeline runs in two environments selected by `BLOKPORT_ENV` (`development` default, or
`production`). Everything env-specific derives from it, so promotion is a CONFIG change, never code.
For a production run set these (a prod run FAILS FAST if the required ones are missing, so it can
never emit unowned products or write into the dev bucket):

```
BLOKPORT_ENV=production
BLOKPORT_S3_BUCKET=<prod staging bucket>        # required — no dev fallback
BLOKPORT_SALES_CHANNEL_ID=<prod sales channel>  # required — products would be channel-less otherwise
BLOKPORT_COMPANY_ID=<prod company>              # required — products would be unowned otherwise
BLOKPORT_IMAGE_MODE=s3                           # stage images to S3 (see below)
BLOKPORT_S3_DRY_RUN=false                        # actually upload images
```

Dev and prod keep SEPARATE `from_medusa/<env>/` and `to_upload/<env>/` (Medusa ids differ per env);
`data/` and `catalog_source/` are shared. See `DEV_PROD_PIPELINE.md` for the full promotion checklist.

## Images & S3 (separate lane)

Image links in the upload files are ALWAYS S3 staging-bucket URLs, never raw supplier URLs, for both
dev and prod. Scraping does NOT stage images — it only records the supplier URLs. The image stage
(`BLOKPORT_IMAGE_MODE=s3`) downloads each source image, de-watermarks/upscales it, uploads it to
`<env>/products/improved/<source>/`, and records the source->improved mapping in
`<env>/products/_manifest.json`. A `build` maps product images through that manifest; an image the
imageproc hasn't processed yet is DROPPED (left blank), never defaulted to its scrape URL. So a
source whose images aren't staged yet shows blank product images until its imageproc run completes,
then a re-`build` fills them in. (Variant textures — `{Key}.png` under `<env>/variations/` — are a
separate generated lane.)

## Key concepts

- **Variant** = a stone (e.g. "Carrara"). Lives in the variant files; uploaded to
  Medusa, which assigns its `Id` (ULID).
- **Alias** = another NAME for a stone ("Bianco Carrara"). Goes in the variant
  files' `Aliases` column -> loaded into Medusa. Adding aliases to existing
  variants is preferred over creating near-duplicate variants.
- **Synonym** = another spelling of an ATTRIBUTE VALUE ("Leather" -> Leathered,
  "First" -> A). Lives in the scraper only (`reference/synonyms/`), never uploaded.
- **Backbone** = per-category tree of allowed combinations (which colour/finish/
  quality each variety may be sold as). Dictates what products can be uploaded.
- **Key** = the unique id of a variant (`slab_marble_carrara_<uuid>`), present in
  every catalog file and the SINGLE join there: backbone <-> export <-> image all match
  on Key. Images are named `{Key}.png` (1:1 with the variant). The category is the
  Key prefix (`slab_`/`block_`/`tile_`). Medusa assigns its own `Id`/ULID on upload.
  The TREE, by contrast, is keyed on Medusa sourceIds (it joins the export by Key to
  the backbone for combos, then maps names->ids via attributes.csv) — so it must be
  (re)built only after the products/export are refreshed against the live DB. `build`
  enforces that ordering for you.

## Categories

Slabs, Blocks, and Tiles are the same material with a different id per format.
Tiles are ACTIVE (their Medusa category id is set in `config/settings.py`).
Tiles MIRROR slabs (same varieties/finishes, deterministic `tile_` Keys); blocks
have their own finishes. A category activates automatically once its `pcat_id` is
set in the registry — see `CATEGORY_GUIDE.md` to add one. Until a category's
variants exist in Medusa, its scraped products gap cleanly (no broken rows).

## After a scrape — just open `to_upload/SYNC_STEPS.md`

It is regenerated every run with the current counts and the exact ordered actions.
That file is the single source of truth for "what do I do now". When in doubt, run
`python -m stone_pipeline.build` and follow it.
