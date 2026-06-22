# Adding a category

Categories live in ONE place — `CATEGORIES` in `stone_pipeline/config/settings.py`.
Everything (matching engines, tree build, image generation, inbox folders, fan-out,
validation, fingerprint) derives from that list, so adding a category is mostly a
registry entry plus its data. A misspelled/unwired category fails loudly rather
than silently becoming a slab.

There are two KINDS of category, set by the behaviour flags:

| flag | stone-format (slab/block/tile) | standalone (e.g. accessories) |
|---|---|---|
| `shares_variety_vocab` | `True` — same stone varieties | `False` — own vocabulary |
| `fan_out` | `True` — a new variety is created here too | `False` |
| `mirror_of` | `"slab"` (mirrors slabs) or `None` (own backbone) | `None` |
| `base_image` | a fal.ai base photo (texture-swap generation) | `""` (real product photos) |

`active` is automatic: a category goes live once its `pcat_id` (Medusa category id)
is set; until then its rows are held in the gap queue, never given another
category's id.

---

## A) A new STONE-FORMAT category (another form of stone)

1. Add the entry to `CATEGORIES`:
   ```python
   Category("xform", "xforms", "Xforms", "<medusa pcat id>", "backbone_xforms.json",
            "<fal.ai base image url>", shares_variety_vocab=True, fan_out=True,
            mirror_of="slab")   # or mirror_of=None to author its own backbone
   ```
2. Build its backbone: if `mirror_of` is set,
   `python -m stone_pipeline.stages.build_tile_backbone` builds every mirror category.
   Otherwise author `catalog_source/backbone_xforms.json` like blocks.
3. Done. Matching, fan-out, the tree, image generation, the inbox folder and
   validation are all handled automatically. Scraped products tagged with this
   format flow through exactly like slabs.

---

## B) A STANDALONE category (e.g. accessories)

Accessories are NOT a form of a stone variety, so they opt out of the stone model.
Add the entry (commented example, fill in when defined):
```python
# Category("accessory", "accessories", "Accessories", "<medusa pcat id>",
#          "accessories_backbone.json", base_image="",
#          shares_variety_vocab=False, fan_out=False, mirror_of=None),
```

What the framework already does for it (NO code needed):
- excludes it from stone-variety fan-out (`fan_out=False`),
- skips texture generation (`base_image=""`) — it uses REAL photos,
- renames real photos dropped in `images/inbox/accessories/` to `{Key}.png`
  (`python -m stone_pipeline.images`),
- HOLDS its scraped rows (`unsupported_category`) instead of mismatching them to a
  stone variety, until you attach its matcher (below),
- builds its tree branch once it has variants + a backbone with an active pcat.

What YOU supply when accessories are defined (the vertical):
1. **Scraper** — `scrapers/<source>.py` on `ScraperBase`, `category="accessory"`
   (see any migrated scraper). Real product photos go to `images/inbox/accessories/`.
2. **Adapter** — `stone_pipeline/adapters/<source>.py`; copy
   `stone_pipeline/adapters/_standalone_template.py` and map the scraped columns to
   the canonical row (set `raw_format="accessory"`). Register it in the adapter
   REGISTRY.
3. **Variants + backbone** — accessory variants (with `accessory_` Keys) go in the
   ONE combined variant file; author `catalog_source/backbone_accessories.json` for
   the accessory hierarchy (it can be simpler than the stone type>finish>colour tree).
4. **Matcher** — register a function in `stages/match_variation.STANDALONE_MATCHERS`:
   ```python
   def match_accessory(row, ref):  # match by name/SKU against accessory variants
       ...                          # set row.variation_id / variation_name, or add a gap
   STANDALONE_MATCHERS["accessory"] = match_accessory
   ```
   This is the only stone-pipeline code change; everything else is data + the usual
   scraper/adapter pattern.

---

## Quick test that the wiring holds
`stone_pipeline/tests/test_categories.py` adds a temporary `accessory` category and
asserts it is not fanned out, not texture-generated, and held (not slab-matched).
Mirror those when you wire the real one.
