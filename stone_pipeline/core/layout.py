"""Per-source run output layout (RunLayout) + the two checklists.

Each source's raw run lands under stone_pipeline/outputs/<run_id>/:
    UPLOAD_STEPS.md            this source's product checklist
    4_products_import/         this source's product list (gathered into to_upload/ by the catalog)
    review/                    products held + rejects + tree gaps
    diagnostics/               canonical parquet, manifest, health

The SHARED catalog (variants, tree, the combined products) is written to the
top-level to_upload/ folder by the catalog stage, NOT here (see write_sync_md).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stone_pipeline.config.settings import ENV_NAME, SETTINGS


@dataclass
class RunLayout:
    root: Path

    @classmethod
    def for_run(cls, outputs_root: Path, run_id: str) -> "RunLayout":
        return cls(root=Path(outputs_root) / run_id)

    # this is the PER-SOURCE run folder (the shared catalog goes to to_upload/ etc.)
    @property
    def products_import(self) -> Path:
        return self.root / "4_products_import"

    @property
    def review(self) -> Path:
        return self.root / "review"

    @property
    def diagnostics(self) -> Path:
        return self.root / "diagnostics"

    @property
    def steps_md(self) -> Path:
        return self.root / "UPLOAD_STEPS.md"

    def ensure(self) -> "RunLayout":
        # per-source product folders; catalog-only folders (variants/backbone/tree)
        # are created on demand by the catalog writers
        for d in (self.products_import, self.review, self.diagnostics):
            d.mkdir(parents=True, exist_ok=True)
        return self


def write_steps_md(layout: RunLayout, *, source: str, run_id: str, health: str,
                   counts: dict) -> Path:
    """Per-source PRODUCTS checklist. The catalog (variants/backbone/tree) is
    shared and synced separately via `python -m stone_pipeline.catalog`; this
    folder is just this source's product list."""
    g = counts.get
    lines = [
        f"# Products — {source}  ({run_id})",
        "",
        f"Health: **{health}**. This folder holds ONLY this source's raw run output.",
        "The products you actually upload are gathered (with every source) into",
        f"`to_upload/{ENV_NAME}/3_products_*.csv`; variants and combinations live there too.",
        "",
        "## To import these products",
        "1. Make sure the catalog is synced: run `python -m stone_pipeline.catalog`,",
        f"   follow `to_upload/{ENV_NAME}/SYNC_STEPS.md` (upload variants → ids → combinations).",
        f"2. Then import this source's products from `to_upload/{ENV_NAME}/3_products_<source>.csv`",
        f"   (or everything at once from `to_upload/{ENV_NAME}/3_products_all.csv`).",
        "",
        f"- {g('emitted', 0)} products ready to import "
        f"({g('products_new', 0)} new, {g('products_existing', 0)} already in Medusa).",
        "- When a Medusa product export is present, the list is split into",
        "  `medusa_import_new.csv` (create) and `medusa_import_existing.csv` (update).",
        f"- {g('inventory_changed', 0)} existing products changed stock → "
        "`inventory_update.csv` (SKU + quantity only, no full re-import).",
        f"- {g('rejected', 0)} not importable yet (their variant/combo is new) — they import on a",
        "  re-run once the catalog is synced. See `review/`.",
        "",
        "## Folders",
        "- `4_products_import/` — the product list to import",
        "- `review/`           — products held + rejects + tree gaps",
        "- `diagnostics/`      — canonical parquet, manifest, health",
        "",
    ]
    layout.steps_md.write_text("\n".join(lines), encoding="utf-8")
    return layout.steps_md


def write_sync_md(*, counts: dict, sources: list[str], products: dict[str, int],
                  inventory: int = 0, discontinued: int = 0) -> Path:
    """The upload checklist, written to to_upload/<env>/SYNC_STEPS.md (next to the files
    it describes). Says what to push to that env's Medusa, in order."""
    g = counts.get
    nprod = sum(products.values())
    lines = [
        f"# Upload to Medusa ({ENV_NAME}) — in this order",
        "",
        f"Consolidated across: {', '.join(sources) or '(none)'}. EVERY file you upload is in",
        f"this folder (to_upload/{ENV_NAME}/). Look before uploading: review/{ENV_NAME}/.",
        f"This is the {ENV_NAME.upper()} set; the other environment has its own folders.",
        "",
        "## 1. Variants  (upload ONE — Medusa upserts on Key, so either is safe)",
        "- `1_variants_full.csv`    the COMPLETE list (first import / full refresh).",
        f"- `1_variants_update.csv`  only the {g('new_variants', 0)} new + {g('confirmed_alias_additions', 0)} "
        "confirmed alias updates (incremental — no need to re-send the full list).",
        f"  ({g('alias_additions', 0) - g('confirmed_alias_additions', 0)} more aliases need a look first — "
        f"`review/{ENV_NAME}/alias_candidates.csv`, NOT in the update file.)",
        f"Then DOWNLOAD Medusa's variant export and SAVE it as  `from_medusa/{ENV_NAME}/variants_export.csv`",
        "(the catalog reads it as the 'existing' set on the next run).",
        "",
        f"## 2. Valid combinations  (AFTER step 1's export is saved to from_medusa/{ENV_NAME}/)",
        "- RE-RUN the whole build so products + combinations pick up the new/refreshed variant ids:",
        "    `python -m stone_pipeline.build`   (run all -> catalog -> combinations -> consistency gate)",
        "  This ONE command rebuilds `2_valid_combinations.csv` from the SAME export as the products",
        "  and fails if anything is stale -- so combinations can never lag the export. (`stone_pipeline.tree`",
        "  still exists for a combinations-only rebuild, but `catalog`/`build` already include it.)",
        "- Upload `2_valid_combinations.csv` BEFORE any products (one row per valid combination:",
        "  product_category_id,type_id,variation_id,finish_id,color_id,quality_id). EVERY imported",
        "  variation is priceable: each gets its category's PRODUCT-USED finishes (the finishes",
        "  products actually carry; tiles mirror slabs) x the colour(s) we know x quality. So it",
        "  only changes when a new VARIANT appears, not when an existing one is first sold.",
        f"  Variations whose type can't be resolved at all land in review/{ENV_NAME}/tree_uncovered_variations.csv.",
        "",
        "## 3. Products  (re-generated by the re-run above)",
        f"- `3_products_all.csv`  — all {nprod} products, OR upload per source:",
        *[f"    `3_products_{s}.csv`  ({n})" for s, n in sorted(products.items())],
        "",
        "## 4. Inventory  (stock-only — INDEPENDENT of products: no re-import, no images)",
        f"- Baseline: DOWNLOAD Medusa's PRODUCT export and SAVE it as  `from_medusa/{ENV_NAME}/products_export.csv`",
        "  (next to attributes.csv + variants_export.csv — all the Medusa downloads stay together). It is the",
        "  'what exists + current stock' reference; the lane only updates EXISTING skus, never creates products.",
        "- Build the delta:  `python -m stone_pipeline.inventory`  (re-scrapes for fresh stock; skips images/products).",
        f"- `4_inventory_update.csv`  — {inventory} existing variant(s) whose stock changed, plus any DISCONTINUED",
        "  products set to stock 0 (a reversible delist). Load via Medusa's INVENTORY importer (Variant Sku +",
        "  Inventory Quantity only). Runs on its own (frequent) cadence; new products flow through steps 1–3.",
        f"- Delete loop: `review/{ENV_NAME}/products_discontinued.csv` lists the {discontinued} product(s) the",
        "  suppliers no longer carry (already stock-0'd above) — review and optionally hard-delete/archive in",
        "  Medusa. The pipeline never auto-deletes.",
        "",
        "## Alongside (apply, but NOT uploaded from here)",
        f"- `catalog_source/backbone_additions/`  — {g('distinct_new_varieties', 0)} new varieties to",
        f"  append to the backbones + backbone_value_updates.csv ({g('backbone_updates', 0)} rows). Apply before the tree.",
        f"- `review/{ENV_NAME}/`  — variants_update_triage, alias_candidates, attribute_synonyms, and",
        f"  images_to_generate ({g('images_to_generate', 0)} `{{Key}}.png` to generate, then upload to S3).",
        "",
    ]
    path = SETTINGS.paths.to_upload_dir / "SYNC_STEPS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
