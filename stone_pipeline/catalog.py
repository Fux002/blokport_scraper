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
import shutil
import sys
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.core.layout import write_sync_md
from stone_pipeline.io import staging
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate, emit, emit_catalog

log = logfmt.get_logger("catalog")


def latest_run_dirs(outputs_root: Path) -> list[Path]:
    """The NEWEST run folder per source under outputs_root. A run id is `<source>_<date>_<time>`,
    so the lexically-greatest folder for a source is its latest scrape; older folders are stale.
    Consolidation MUST ignore the stale ones — each scrape is a full snapshot, so including two
    run folders for one source would double-count it (duplicate SKUs). This makes the catalog
    idempotent and leftover-proof: a forgotten old run folder can never inflate the output."""
    latest: dict[str, Path] = {}
    for d in sorted(Path(outputs_root).glob("*_*_*")):   # <source>_<YYYYMMDD>_<HHMMSS>
        if d.is_dir():
            latest[d.name.rsplit("_", 2)[0]] = d          # sorted asc -> newest wins
    return list(latest.values())


def find_canonical(outputs_root: Path) -> list[Path]:
    return [c for d in latest_run_dirs(outputs_root)
            if (c := d / "diagnostics" / "canonical.parquet").exists()]


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
    inventory = collect_inventory(outputs_root)   # -> to_upload/4_inventory_update.csv (existing stock only)
    discontinued = collect_discontinued(outputs_root)  # -> review/4 delete-loop report (stock-0'd in inventory)
    sync = write_sync_md(counts=result.counts, sources=sorted(set(sources)),
                         products=products, inventory=inventory, discontinued=discontinued)
    images_queued = _auto_queue_images()          # new variants -> prompts_to_generate.json (auto)
    held = gate_on_images()                        # hold new variants with no S3 image out of the upload
    pruned = prune_superseded_runs(outputs_root)  # leave only the latest run folder per source
    log.info("catalog consolidated", extra={"extra_fields": {
        "sources": sorted(set(sources)), **result.counts,
        "products": sum(products.values()), "inventory": inventory,
        "discontinued": discontinued, "pruned_stale_runs": pruned}})
    return sync


def _auto_queue_images() -> int:
    """Auto-build the generator's prompt queue (image_pipeline/prompts_to_generate.json) for the
    new variants this run, then — if FAL_KEY is set — kick off generation so the image is ready on
    S3 before import. Without FAL_KEY it just leaves the queue ready for the image_pipeline run.
    The variant Image URL already points at dev/variations/{Key}.png, so generation overwrites that
    exact object (one image per variant)."""
    import json
    import os

    from stone_pipeline.stages import image_prompts
    try:
        prompts_path = image_prompts.build()
    except Exception:
        log.exception("image prompt queue build failed")
        return 0
    items = json.loads(prompts_path.read_text(encoding="utf-8")) if prompts_path.exists() else []
    items = items if isinstance(items, list) else items.get("items", [])
    if items and os.environ.get("FAL_KEY"):
        log.info("auto image generation: FAL_KEY present, generating", extra={"extra_fields": {"queued": len(items)}})
        _generate_queued_images()
    elif items:
        log.info("image prompt queue ready (set FAL_KEY + run image_pipeline to generate)",
                 extra={"extra_fields": {"queued": len(items)}})
    return len(items)


def _generate_queued_images() -> None:
    """Run the existing image_pipeline chain (FLUX.2 max -> BEN2 -> S3) on the queued prompts.
    Reuses the committed scripts so the S3 step you set up elsewhere stays the single source."""
    import subprocess
    ip = SETTINGS.paths.workspace_root / "image_pipeline"
    for script in ("genetate_images.py", "rb_images.py"):
        if (ip / script).exists():
            subprocess.run(["python", script], cwd=ip, check=False)


def _s3_image_checker():
    """Return a callable Key -> bool that checks <env>/variations/{Key}.png on the staging bucket,
    or None when S3 isn't reachable (dry-run / no boto3 / local dev) so the gate simply doesn't
    engage. On AWS it head_objects the real bucket."""
    from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS as _S
    s3 = _S.s3
    if s3.dry_run:
        return None
    try:
        import boto3
    except ImportError:
        return None
    client = boto3.Session(profile_name=s3.credentials_profile, region_name=s3.region).client("s3")
    prefix = f"{ENV_SEGMENT}/variations/"

    def exists(key: str) -> bool:
        try:
            client.head_object(Bucket=s3.bucket, Key=f"{prefix}{key}.png")
            return True
        except Exception:
            return False
    return exists


def gate_on_images(checker=None, to_upload: Path | None = None, export_file: Path | None = None) -> int:
    """Enforce the invariant 'a variant is in the upload file ⟺ its image is on S3'. After image
    generation, any genuinely-new variant (in 1_variants_update, not in the Medusa export) whose
    {Key}.png is NOT on S3 is dropped from BOTH upload files — it stays queued and joins the upload
    once its image exists. No-op when S3 can't be checked, so local/dry-run output is unchanged."""
    to_upload = Path(to_upload or SETTINGS.paths.to_upload_dir)
    export_file = Path(export_file or SETTINGS.paths.export_file)
    upd = to_upload / "1_variants_update.csv"
    if not upd.exists():
        return 0
    export_keys: set[str] = set()
    if export_file.exists():
        with export_file.open(encoding="utf-8-sig", newline="") as h:
            export_keys = {(r.get("Key") or "").strip() for r in csv.DictReader(h)}
    with upd.open(encoding="utf-8-sig", newline="") as h:
        new_keys = {(r.get("Key") or "").strip() for r in csv.DictReader(h)
                    if (r.get("Key") or "").strip() and r["Key"] not in export_keys}
    checker = checker if checker is not None else _s3_image_checker()
    if checker is None or not new_keys:
        return 0
    missing = {k for k in new_keys if not checker(k)}
    if not missing:
        return 0
    for fname in ("1_variants_update.csv", "1_variants_full.csv"):
        p = to_upload / fname
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        header, body = rows[0], [r for r in rows[1:] if r and r[0] not in missing]
        with p.open("w", newline="", encoding="utf-8") as h:
            csv.writer(h).writerows([header, *body])
    log.warning("held new variants with no S3 image out of the upload (still queued)",
                extra={"extra_fields": {"held": len(missing)}})
    return len(missing)


def prune_superseded_runs(outputs_root: Path) -> int:
    """Remove run folders that a newer run for the SAME source has superseded, so outputs/ keeps
    exactly one (current) snapshot per source and stale data can't accumulate. The latest run per
    source is always kept; the dedicated _inventory/ tree (no <source>_<date>_<time> shape at this
    level) is untouched. Returns the number of folders removed."""
    keep = {d.resolve() for d in latest_run_dirs(outputs_root)}
    removed = 0
    for d in Path(outputs_root).glob("*_*_*"):
        if d.is_dir() and d.resolve() not in keep:
            shutil.rmtree(d)
            removed += 1
    return removed


def collect_products(outputs_root: Path) -> dict[str, int]:
    """Gather each source's product import CSV into to_upload/: one file per source
    (3_products_<source>.csv) plus a combined all-sources file (3_products_all.csv),
    same 45-column Medusa schema. So the products are next to the variants and tree."""
    to_upload = SETTINGS.paths.to_upload_dir
    to_upload.mkdir(parents=True, exist_ok=True)
    # prune stale per-source files first so a source that no longer runs cannot leave a
    # 3_products_<gone>.csv lingering in the upload set (3_products_all is rewritten below).
    for old in to_upload.glob("3_products_*.csv"):
        old.unlink()
    header: list[str] | None = None
    all_rows: list[list[str]] = []
    counts: dict[str, int] = {}
    for run_dir in latest_run_dirs(outputs_root):          # newest run per source only
        src_csv = run_dir / "4_products_import" / "medusa_import.csv"
        if not src_csv.exists():
            continue
        source = run_dir.name.rsplit("_", 2)[0]            # <source>_<date>_<time> -> <source>
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


def consolidate_inventory(inv_csvs: list[Path], to_upload: Path | None = None) -> int:
    """Merge the given per-run inventory_update.csv files into ONE deliverable:
    to_upload/<env>/4_inventory_update.csv. Deduped by Variant Sku (globally unique), last run
    wins — so re-running against the same Medusa baseline never double-lists a SKU. Medusa's
    INVENTORY importer loads it WITHOUT a product re-import or any image work. Always written
    (header-only when nothing changed) so the deliverable is predictable. Returns SKU count."""
    to_upload = Path(to_upload or SETTINGS.paths.to_upload_dir)
    to_upload.mkdir(parents=True, exist_ok=True)
    by_sku: dict[str, list[str]] = {}
    for src_csv in inv_csvs:
        if not src_csv.exists():
            continue
        with src_csv.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        for r in rows[1:]:          # skip header
            if r and r[0].strip():
                by_sku[r[0].strip().upper()] = r   # Variant Sku is column 0
    with (to_upload / "4_inventory_update.csv").open("w", newline="", encoding="utf-8") as h:
        writer = csv.writer(h)
        writer.writerow(emit.INVENTORY_COLUMNS)
        writer.writerows(by_sku.values())
    return len(by_sku)


def collect_inventory(outputs_root: Path) -> int:
    """Catalog path: consolidate the latest run per source's inventory delta."""
    return consolidate_inventory(
        [d / "4_products_import" / "inventory_update.csv" for d in latest_run_dirs(outputs_root)])


def collect_discontinued(outputs_root: Path) -> int:
    """Consolidate the latest run per source's delete-loop report into one review file:
    review/<env>/products_discontinued.csv (deduped by SKU). These are products the suppliers no
    longer carry — already set to stock 0 by the inventory update, listed here for optional delete.
    Always written (header-only when none) so the file is a predictable, auditable deliverable."""
    review = SETTINGS.paths.review_dir
    review.mkdir(parents=True, exist_ok=True)
    by_sku: dict[str, list[str]] = {}
    for d in latest_run_dirs(outputs_root):
        p = d / "review" / "products_discontinued.csv"
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.reader(h))
        for r in rows[1:]:
            if r and r[0].strip():
                by_sku[r[0].strip().upper()] = r
    with (review / "products_discontinued.csv").open("w", newline="", encoding="utf-8") as h:
        writer = csv.writer(h)
        writer.writerow(emit.DISCONTINUED_COLUMNS)
        writer.writerows(by_sku.values())
    return len(by_sku)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    outputs_root = Path(argv[0]) if argv else SETTINGS.paths.outputs_dir
    steps = run(outputs_root)
    print(f"upload checklist: {steps}")
    print(f"  variants:  {steps.parent / '1_variants_full.csv'}  (or 1_variants_update.csv)")
    print(f"  products:  {steps.parent / '3_products_all.csv'}")
    print(f"  inventory: {steps.parent / '4_inventory_update.csv'}  (existing products' stock only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
