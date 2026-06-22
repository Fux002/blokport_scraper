"""Catalog sync CLI: consolidate the new variants, aliases, backbone entries, and
images from EVERY source run into one shared catalog folder, with a sync
checklist. The catalog (variants/backbone/tree/images) is shared across sources;
products are per-source. Run this after scraping, before importing products.

    python -m stone_pipeline.run all      # scrape every source
    python -m stone_pipeline.catalog      # build to_upload/ + review/
    # follow to_upload/SYNC_STEPS.md ; then python -m stone_pipeline.tree

It reads every run's diagnostics/canonical.parquet, so it reflects all sources at
once and de-duplicates a variant that several suppliers carry.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.core.layout import write_sync_md
from stone_pipeline.io import staging
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate, emit_catalog

log = logfmt.get_logger("catalog")


def find_canonical(outputs_root: Path) -> list[Path]:
    return sorted(Path(outputs_root).glob("*/diagnostics/canonical.parquet"))


def run(outputs_root: Path | None = None) -> Path:
    outputs_root = Path(outputs_root or SETTINGS.paths.outputs_dir)
    parquets = find_canonical(outputs_root)
    if not parquets:
        raise SystemExit(f"no source runs found under {outputs_root} (run `python -m stone_pipeline.run all` first)")

    rows = []
    sources: list[str] = []
    for p in parquets:
        src_rows = staging.read_canonical(p)
        rows.extend(src_rows)
        if src_rows:
            sources.append(src_rows[0].src_site)

    ref = loaders.load_all()
    result = curate.run(rows, ref)                # -> to_upload/1_variants_update.csv, review/, backbone_additions/
    emit_catalog.build()                          # -> to_upload/1_variants_full.csv (the complete file)
    products = collect_products(outputs_root)     # -> to_upload/3_products_*.csv (per source + combined)
    sync = write_sync_md(counts=result.counts, sources=sorted(set(sources)), products=products)
    log.info("catalog consolidated", extra={"extra_fields": {
        "sources": sorted(set(sources)), **result.counts, "products": sum(products.values())}})
    return sync


def collect_products(outputs_root: Path) -> dict[str, int]:
    """Gather each source's product import CSV into to_upload/: one file per source
    (3_products_<source>.csv) plus a combined all-sources file (3_products_all.csv),
    same 45-column Medusa schema. So the products are next to the variants and tree."""
    to_upload = SETTINGS.paths.to_upload_dir
    to_upload.mkdir(parents=True, exist_ok=True)
    header: list[str] | None = None
    all_rows: list[list[str]] = []
    counts: dict[str, int] = {}
    for src_csv in sorted(outputs_root.glob("*/4_products_import/medusa_import.csv")):
        source = src_csv.parent.parent.name.rsplit("_", 2)[0]   # <source>_<date>_<time> -> <source>
        with src_csv.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        if not rows:
            continue
        header, body = rows[0], rows[1:]
        counts[source] = len(body)
        with (to_upload / f"3_products_{source}.csv").open("w", newline="", encoding="utf-8") as h:
            csv.writer(h).writerows([header, *body])
        all_rows += body
    if header is not None:
        with (to_upload / "3_products_all.csv").open("w", newline="", encoding="utf-8") as h:
            csv.writer(h).writerows([header, *all_rows])
    return counts


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    outputs_root = Path(argv[0]) if argv else SETTINGS.paths.outputs_dir
    steps = run(outputs_root)
    print(f"upload checklist: {steps}")
    print(f"  variants:  {steps.parent / '1_variants_full.csv'}  (or 1_variants_update.csv)")
    print(f"  products:  {steps.parent / '3_products_all.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
