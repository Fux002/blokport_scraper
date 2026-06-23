# Runbook — how the scraper keeps Medusa in sync and imports products

Read this if you forget how the whole thing works. It explains the flow, where
the files live, and what you do after a scrape.

## The one-line version

The scraper turns messy supplier scrapes into a Medusa product import. Before the
products can be imported, Medusa's catalog (variants, aliases, the allowed-combo
tree) must contain everything the products reference. The pipeline produces every
file you upload in ONE folder — `to_upload/` — with a checklist (`SYNC_STEPS.md`).

## Run it

```bash
python -m stone_pipeline.run all          # 1. scrape every source -> per-source products
python -m stone_pipeline.catalog          # 2. build to_upload/<env>/ (variants + products) + review/<env>/
python -m stone_pipeline.tree             # 3. build to_upload/<env>/2_valid_combinations.csv (after the export)
# then follow to_upload/<env>/SYNC_STEPS.md

# <env> is development by default. For the production set, prefix every command:
#   BLOKPORT_ENV=production python -m stone_pipeline.catalog   (etc.)
# The scrape (step 1, data/) is shared, so only re-run steps 2-3 for the second env.
```

## Where the files live — four top-level folders

```
SHARED across environments (the "core"):
  data/              ← the raw scrape (env-independent)
  catalog_source/    ← the data you MAINTAIN by hand (backbones; names, not Medusa ids)
PER ENVIRONMENT — development/ and production/, selected by BLOKPORT_ENV
(the Medusa pcat / attribute / variation ids differ per env, so each has its own set):
  from_medusa/<env>/ ← SAVE that env's Medusa downloads here (variants_export, attributes); READ-only
  to_upload/<env>/   ← PRODUCED by the pipeline; UPLOAD these to that env's Medusa (numbered order)
  review/<env>/      ← look before uploading; never uploaded
```
You scrape ONCE (shared), then build the catalog/combinations per env:
`BLOKPORT_ENV=development` (default) and `BLOKPORT_ENV=production` each read/write their own
`from_medusa/<env>/` + `to_upload/<env>/`. Everything below shows paths relative to one `<env>/`.

### `to_upload/`  — everything you push to Medusa, in number order
```
1_variants_full.csv      complete variant list (first / full import)
1_variants_update.csv    only the new + alias updates (incremental; Medusa upserts on Key)
2_valid_combinations.csv one row per valid combination (build after the export; upload BEFORE products)
3_products_all.csv       every source's products combined
3_products_<source>.csv  per source (set a company per supplier in config/sources.yaml)
SYNC_STEPS.md            the checklist, with counts
```

### `from_medusa/`  — what you download from Medusa
```
variants_export.csv      the variant export (Id + Key); the catalog reads it as the "existing" set
products_export.csv      (optional) the product export, to split new vs existing products
attributes.csv           colour / finish / type / quality / category  →  Medusa id   (your "colour/type" file)
```

### `catalog_source/`  — maintained by hand
```
backbone_slabs.json      allowed combos per category (append new varieties here)
backbone_blocks.json · backbone_tiles.json
backbone_additions/      per run: the new varieties + value changes to apply to the backbones
ports.csv · missing_variants.csv · image_model.csv
```

### `review/`  — decide before uploading (never uploaded)
```
variants_update_triage.csv  alias-vs-new, with nearest match + score
alias_candidates.csv · attribute_synonyms.csv
images_to_generate.csv      the {Key}.png to generate (see image_pipeline/)
```

The catalog is a **pure function**: it READS the immutable `from_medusa/variants_export.csv`
("existing") and WRITES `to_upload/1_variants_*.csv` — the two never alias, so re-running
gives byte-identical files. No file is hand-edited; there is no "reset" step.

(The raw per-source run output — canonical data, diagnostics, the un-gathered product
CSV — stays under `stone_pipeline/outputs/<source>_*/`; you never upload from there.)

## The flow (what actually happens)

```
python -m stone_pipeline.run all   → per-source products (stone_pipeline/outputs/<source>_*/)
python -m stone_pipeline.catalog   → to_upload/ (variants_full + update, products_*) + review/
                                       + catalog_source/backbone_additions/

UPLOAD (per to_upload/SYNC_STEPS.md):
   1. Upload to_upload/1_variants_full.csv (or 1_variants_update.csv) → Medusa makes ids
      → download the export, SAVE AS from_medusa/variants_export.csv
   2. Apply catalog_source/backbone_additions/* to the backbones
   3. RE-RUN so the products + combinations pick up the new variant ids, then build:
        python -m stone_pipeline.run all && python -m stone_pipeline.catalog
        python -m stone_pipeline.tree         → to_upload/2_valid_combinations.csv
   4. Upload 2_valid_combinations.csv (BEFORE products), then to_upload/3_products_all.csv (or per source)
   (images, anytime: generate review/images_to_generate.csv → {Key}.png → S3)
```

Step 3's re-run matters: a product's variation sourceId comes from the export, so after
the export changes (new ids), the products must be regenerated against it BEFORE the
combinations — otherwise both carry stale ids. The output is `2_valid_combinations.csv`:
one row per valid combination (`product_category_id,type_id,variation_id,finish_id,
color_id,quality_id`) for Medusa's relational `valid_combination` table — flat rows, no
size cap. EVERY imported variation is made priceable, so it only changes when a brand-new
VARIANT appears (not when an existing variation is first sold). Each variation gets its
category's PRODUCT-USED finish set (the finishes that category's products actually carry;
tiles mirror slabs) × the colour(s) we know × quality. Colour/type come from the best
source available: the product that sold it, its backbone post, a same-variety product in
another category (fan-out mirrors inherit the scraped colour), or the type parsed from its
Key/Name + the catalogue's default colour. The few variations whose type can't be resolved
at all are listed in `review/tree_uncovered_variations.csv` (covered once a scrape
classifies them).

It is **iterative**: a product whose variant was just created only gets its variation
id after step 1's export, so re-run the scrape after syncing and the previously-held
products now emit. Nothing is ever guessed — a product only appears when every id it
needs already resolves.

## Key concepts

- **Variant** = a stone (e.g. "Carrara"). Lives in the variant files; uploaded to
  Medusa, which assigns its `Id` (ULID).
- **Alias** = another NAME for a stone ("Bianco Carrara"). Goes in the variant
  files' `Aliases` column → loaded into Medusa. Adding aliases to existing
  variants is preferred over creating near-duplicate variants.
- **Synonym** = another spelling of an ATTRIBUTE VALUE ("Leather" → Leathered,
  "First" → A). Lives in the scraper only (`reference/synonyms/`), never uploaded.
- **Backbone** = per-category tree of allowed combinations (which colour/finish/
  quality each variety may be sold as). Dictates what products can be uploaded.
- **Key** = the unique id of a variant (`slab_marble_carrara_<uuid>`), present in
  every catalog file and the SINGLE join there: backbone ↔ export ↔ image all match
  on Key. Images are named `{Key}.png` (1:1 with the variant). The category is the
  Key prefix (`slab_`/`block_`/`tile_`). Medusa assigns its own `Id`/ULID on upload.
  The TREE, by contrast, is keyed on Medusa sourceIds (it joins the export by Key to
  the backbone for combos, then maps names→ids via attributes.csv) — so it must be
  (re)built only after the products/export are refreshed against the live DB.

## Categories

Slabs, Blocks, and Tiles are the same material with a different id per format.
Tiles are now ACTIVE (their Medusa category id is set in `config/settings.py`).
Tiles MIRROR slabs (same varieties/finishes, deterministic `tile_` Keys); blocks
have their own finishes. A category activates automatically once its `pcat_id` is
set in the registry — see `CATEGORY_GUIDE.md` to add one. Until a category's
variants exist in Medusa, its scraped products gap cleanly (no broken rows).

## After a scrape — just open `to_upload/SYNC_STEPS.md`

It is regenerated every run with the current counts and the exact ordered actions.
That file is the single source of truth for "what do I do now".
