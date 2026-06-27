# catalog_source/ — the catalog data you MAINTAIN

The hand-maintained source the pipeline READS to build the catalog. (Things you
upload are in `../to_upload/`; Medusa downloads go in `../from_medusa/`.) The exact
filenames are pinned in `stone_pipeline/config/settings.py`.

- `attributes.csv` lives in `../from_medusa/` now (its `sourceid`s come FROM Medusa,
  like the variants export — `category, value, sourceid`, incl. a `category,<Name>,<pcat>`
  row per product category). Add a new colour/finish/type/category there with its
  Medusa id after you create the value in Medusa.

- `backbone_slabs.json`, `backbone_blocks.json`, `backbone_tiles.json` — per-category
  "backbone": the allowed combinations (which colour/finish/quality each variety may
  be sold as), each post carrying its `key`. Tiles MIRROR slabs (rebuild with
  `python -m stone_pipeline.stages.build_tile_backbone`).

- `backbone_additions/` — PRODUCED each catalog run: the new varieties to append to
  the backbones above (`slab.json` / `block.json` / `tile.json`) plus
  `backbone_value_updates.csv` (existing varieties missing a colour/finish). Apply
  these to the backbones before building the tree.

- `ports.csv` — the MASTER list of shipping ports (id, name, un_locode, country_iso, …).
  Assign which ports each supplier ships from in `config/sources.yaml` `ports_default`,
  by port NAME or UN/LOCODE (e.g. `Brindisi` / `ITBDS`) — resolved to ids against this file.
- `origin_map.csv` — the per-VARIETY quarry country (origin is a property of the stone, NOT the
  supplier — a trader in Italy sells Brazilian quartzite; origin is Brazil). Columns
  `match_type,pattern,country_iso,city,county`, two rule kinds:
  - `variety,<Variant Name>,<ISO>,…` — an EXACT name → country (type-independent). Add one row
    per variety whose origin you know.
  - `pattern,<token>,<ISO>,,` — a single name TOKEN that points at one country (`carrara`→IT,
    `persa`→BR); matches a whole word, so it carries origin onto BRAND-NEW variants whose name
    contains a known place/family token — no per-variant entry needed.
  Resolution order (`derive_origin`): scraped country → this file (exact, then pattern) → the
  supplier's `origin_default` as a LOW-confidence **flagged** fallback. That `origin_supplier_default`
  flag in `products_review.csv` is the maintenance worklist: it lists exactly which new varieties
  have no real origin yet — add a `variety,…` row here and the next build resolves them exactly.
  (This file is hand-maintained — it is NOT generated from any export.)
- `missing_variants.csv` — variants parked OUT of the tree (no/ambiguous image).
- `image_model.csv` — which model made each variant's image (`flux-2-max` vs `legacy`);
  the redo-on-max worklist.

## Naming convention (so dev and prod match)
- Variant Key: `{slab|block|tile}_{type}_{name}_{uuid}` (uuid deterministic for new ones)
- Image: `{Key}.png` at `<bucket>/dev/variations/{Key}.png`
- Only the bucket base and the Medusa ids differ between dev and prod.
