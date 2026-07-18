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
  `match_type,pattern,country_iso,city,county,confirmed,stone_type`, two rule kinds:
  - `variety,<Variant Name>,<ISO>,…` -- an EXACT name to a country. Add one row per variety whose origin
    you know. By default a `variety` row is TYPE-BLIND (applies to the name under any stone type).
  - `pattern,<token>,<ISO>,,` — a single name TOKEN (`carrara`→IT, `persa`→BR). **Patterns are a
    SUGGESTION only now** — they pre-fill the :4200 mint origin field but are NEVER emitted as a
    product's origin (a look-alike named after a famous stone is not from that country).
  - `confirmed` — `true` once you have VERIFIED a `variety` row's country. A blank/absent `confirmed`
    means the row is the old unverified snapshot: it still ships (Medusa needs an origin) but carries
    the `origin_unconfirmed` review flag until you confirm it. Optional column — a CSV without it loads
    every row as unconfirmed.
  - `stone_type` -- OPTIONAL, the LAST column. Blank = type-blind (the default, and how every existing
    row loads; a CSV without the column is unchanged). Set it only for a HOMONYM: the same variety name
    used for genuinely different stones of different types, which can have different origins. A
    type-scoped row wins for that type; the type-blind row is the fallback for every other type. Example
    (columns `match_type,pattern,country_iso,city,county,confirmed,stone_type`):
    `variety,Aqua Blue,BR,,,true,` (any type → Brazil) plus `variety,Aqua Blue,IR,,,true,Onyx` (the Onyx
    one → Iran). Operator mints carry the type too, so a minted origin only overrides its own type.
  Resolution order (`derive_origin`): scraped country → **operator-minted origin** (the country picked
  on :4200 at mint, overlaid here as a confirmed rule) → this file, EXACT only (`confirmed`→clean,
  unconfirmed→flagged) → the supplier's `origin_default` as a LOW-confidence **flagged** fallback.
  Maintenance loop: the `origin_unconfirmed` / `origin_supplier_default` flags in `products_review.csv`
  are the worklist — confirm or add a `variety,…,true` row here and the next build resolves it clean.
  New varieties get their true origin at mint on :4200 (part of the approval), never guessed from the name.
  (This file is hand-maintained — it is NOT generated from any export.)
- `missing_variants.csv` — variants parked OUT of the tree (no/ambiguous image).
- `image_model.csv` — which model made each variant's image (`flux-2-max` vs `legacy`);
  the redo-on-max worklist.

## Naming convention (so dev and prod match)
- Variant Key: `{slab|block|tile}_{type}_{name}_{uuid}` (uuid deterministic for new ones)
- Image: `{Key}.png` at `<bucket>/dev/variations/{Key}.png`
- Only the bucket base and the Medusa ids differ between dev and prod.
