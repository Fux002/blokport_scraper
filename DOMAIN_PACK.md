# Domain packs: running the pipeline for a different product

The pipeline logic is product-agnostic. Everything stone-specific lives in a **domain pack**
(`stone_pipeline/config/domains/<name>.yaml`), selected at startup by `BLOKPORT_DOMAIN_PACK`
(default `stone`). A different product type is a sibling pack plus its own data and deployment,
never a code edit. The pack is loaded once and cached (`config/domain.active_pack()`), validated
loud at load (`_validate_shape`), and read by every stage.

## What the pack declares

Vocabulary and defaults (see `config/domains/stone.yaml` for the canonical, fully-commented example):

- `attributes` / `disambiguator` / `leaf_attributes` - the Medusa attribute set, the identity
  attribute that drives the Key, and the leaf-growing attributes.
- `categories` - the category model (one FORM of the product). Each carries behavioural role flags:
  - `default_form: true` (exactly one) - the branch a row falls back to when its format is
    unresolved, and the finish/dimension fallback source. Stone: `slab`.
  - `bulk_form: true` (at most one) - the uncut/solid form; drives `is_block` and the block-scale
    dimension/freight branch. A material with no such form declares it nowhere. Stone: `block`.
  - `mirror_of: <category>` - this category inherits the mirrored one's finishes. Stone: `tile` mirrors `slab`.
- `dimension_ranges` / `dimension_defaults` / `in_stock_fallback_qty` - per-category (every category
  needs an entry in each; pack-validated).
- `generic_descriptors` / `generic_material_word` / `ambiguous_type_words` - name/alias vocabulary.
- `default_finishes` / `fallback_color` / `last_resort_finishes` / `last_resort_quality` /
  `block_finish` / `finish_phrases` - attribute defaults and description phrasing.
- **Name-cleaning corpus rules** (optional; default "never mangle"):
  - `name_code_pattern` - a regex for codey-LOOKING tokens that are real names here (stone: granite `G682`).
  - `trailing_grade_letters` - whether a trailing lone letter is a grade code to strip/flag (stone: `Rosal C` -> `Rosal`).
- `classify_texture_color` (default `true`) - classify a variety's colour from its product image with
  the stone-tuned CV palette. A material whose tones that palette cannot read sets it `false` and takes
  the fallback/Medusa colour instead.

The pack validator fails loud at load on an internally inconsistent pack (a category missing from a
per-category map, a `disambiguator` outside `attributes`, no `default_form`, two `bulk_form`s).

## The unit of deployment is ONE material per deployment

Nothing namespaces the pack **on disk**: `catalog_source/`, `from_medusa/<env>/`, `to_upload/<env>/`,
`config.db`, the ledger DB, and the S3 key prefixes are keyed by **environment**, not by material.
Two materials in one environment would collide on every one of those paths.

So the real onboarding unit is **one deployment per material**: a wood build is its own DEPLOYMENT
(its own S3 bucket + namespace, `config.db`, ledger and ECS task) running with
`BLOKPORT_DOMAIN_PACK=wood`. Stone and wood never share a store. (Co-tenanting several materials in one
deployment would require namespacing all of the above by material - a larger change, deliberately not
done.)

> **`BLOKPORT_ENV` is NOT the isolation mechanism - do not invent a value for it.** It is the
> deployment TIER and nothing else: `development` or `production` (plus the `dev`/`prod` aliases).
> Every production guard keys off it, so a brand- or material-prefixed value like
> `wudport-production` used to read as "not production" and silently downgrade the whole run to
> development semantics: dev S3 prefix, `BLOKPORT_S3_DRY_RUN` defaulting true, and the bucket +
> sales-channel guards disabled. `config/settings.py` now validates the value against that closed
> set and **raises at import** on anything else (`stone_pipeline/tests/test_env_tier.py` pins it),
> so the mistake fails loudly rather than quietly - but the rule still stands:
>
> **Isolation comes from the separate bucket, task, `config.db` and ledger. The tier stays real.**
> A wood PRODUCTION deployment sets `BLOKPORT_ENV=production`, exactly like the stone one.

## Onboarding a new material

Three tracks (details and the full gap analysis in `MATERIAL_AGNOSTIC_REVIEW_2026-08-14.md`):

1. **Data plane** (real authoring, not a toggle): the material's `attributes.csv` values in Medusa, a
   backbone/seed, synonyms, and density/area data - plus its own env / S3 namespace / `config.db` / ledger.
2. **Code** (already generalised): pack-driven category roles, name heuristics, validators, colour flag.
3. **The gate**: a small `<material>.yaml` + a ~10-row fixture run end to end (scrape -> derive -> emit),
   eyeballed for right categories, un-mangled names, correct dimensions/density, nothing wrongly held.
   **Code review does not prove a new material works - only a clean smoke run does.**
