"""Inventory-only sync: refresh supplier stock for EXISTING products, without re-importing
products or touching images.

The catalog (variants + products + images) and the inventory level are two different
deliverables on two different clocks: products change occasionally (and cost an image
re-render), supplier stock changes often. This command is the stock clock. It runs the same
scrape -> match -> classify path as a full run, but skips the image stage and the
product-import/canonical writes, emitting only the inventory delta for existing products whose
stock moved -> to_upload/<env>/4_inventory_update.csv (Variant Sku + Inventory Quantity).

    python -m stone_pipeline.inventory            # all sources
    python -m stone_pipeline.inventory varsha     # one source
    python -m stone_pipeline.inventory all        # explicit all

It reads the latest scrape per source, so scrape first (or on the same cron) for fresh stock.
"How much exists" is the Medusa product export in from_medusa/<env>/ (read-only here); upload
the file, then re-download the export to advance the baseline. New products still flow through
`run all` + `catalog` + `tree`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from stone_pipeline import catalog
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.run import run_all

log = logfmt.get_logger("inventory")


def run(sources: list[str] | None = None, outputs_dir: Path | None = None) -> tuple[Path, int]:
    """Refresh inventory for the given sources (all by default). Returns (deliverable, sku_count)."""
    # a dedicated output root so inventory runs never mix with full-run folders and stay invisible
    # to the catalog glob (outputs/*/...), which is one level shallower than outputs/_inventory/<run>/
    out_root = Path(outputs_dir) if outputs_dir else SETTINGS.paths.outputs_dir / "_inventory"
    results = run_all(sources, outputs_dir=out_root, inventory_only=True)
    inv_csvs = [out_root / m.run_id / "4_products_import" / "inventory_update.csv"
                for m in results.values()]
    count = catalog.consolidate_inventory(inv_csvs)
    discontinued = catalog.collect_discontinued(out_root)   # delete-loop report (stock-0'd above)
    path = SETTINGS.paths.to_upload_dir / "4_inventory_update.csv"
    log.info("inventory consolidated", extra={"extra_fields": {
        "sources": sorted(results), "changed_skus": count, "discontinued": discontinued,
        "file": str(path)}})
    return path, count


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    positional = [a for a in argv if not a.startswith("-")]
    sources = None if (not positional or positional == ["all"]) else positional
    path, count = run(sources)
    print(f"inventory update: {path}  ({count} variant(s) with changed stock)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
