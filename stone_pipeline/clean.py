"""Housekeeping: remove SUPERSEDED working data so only the current state remains.

Each scrape and each pipeline run writes a fresh timestamped folder; the pipeline always uses the
latest, so older ones are pure leftover (superseded snapshots of the same source). This prunes
them — keeping the LATEST per source — plus stale inventory runs, OS cruft (.DS_Store), and
ORPHAN generated images (image_pipeline/{images,to_upload}/<Key>.png whose variant Key is no
longer in the catalog — a stone that was removed or renamed).

    python -m stone_pipeline.clean            # report + remove
    python -m stone_pipeline.clean --dry-run  # report only, remove nothing

NEVER touches: to_upload/ (deliverables), from_medusa/ (Medusa downloads), catalog_source/
(source of truth), review/, images/inbox (your drop folder), or any generated image whose Key
IS still in the catalog. Latest scrape + latest run per source are always kept, and a missing/
empty catalog skips image pruning entirely, so the pipeline never loses an input or a live image.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

from stone_pipeline.catalog import latest_run_dirs
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("clean")


def _scrape_is_complete(folder: Path) -> bool:
    """Mirror run.find_scrape_file: a folder is usable only if it actually CONTAINS products.csv (the
    pipeline ingests `{source}/*/products.csv`) AND its marker doesn't say incomplete. A crashed run
    leaves products.csv-less / markerless folders -- those must NOT be treated as the authoritative
    keeper, or clean would delete the older complete folder the pipeline still ingests."""
    if not (folder / "products.csv").exists():
        return False
    marker = folder / "scrape_complete.json"
    if not marker.exists():
        return True   # legacy folder with products.csv but no marker is accepted
    try:
        return bool(json.loads(marker.read_text(encoding="utf-8")).get("complete", True))
    except (ValueError, OSError):
        return True


def _superseded_scrapes(data_dir: Path) -> list[Path]:
    """data/<source>/<ts>/ folders that are NOT the latest for their source. Keeps the newest AND the
    AUTHORITATIVE folder the pipeline actually ingests (run.find_scrape_file uses the latest COMPLETE
    folder, skipping a truncated newest) -- so when the newest scrape is incomplete we never delete
    the older complete folder the pipeline still depends on."""
    stale: list[Path] = []
    for src_dir in sorted(p for p in data_dir.glob("*") if p.is_dir()):
        runs = sorted(d for d in src_dir.glob("*") if d.is_dir())
        if not runs:
            continue
        keep = {runs[-1]}                                   # always keep the newest
        for d in reversed(runs):                            # ...plus the latest usable (complete) one
            if _scrape_is_complete(d):
                keep.add(d)
                break
        stale.extend(d for d in runs if d not in keep)
    return stale


def _superseded_runs(root: Path) -> list[Path]:
    """outputs/<env>/<source>_<ts>/ folders a newer run for the same source has superseded."""
    if not root.exists():
        return []
    keep = {d.resolve() for d in latest_run_dirs(root)}
    return [d for d in root.glob("*_*_*") if d.is_dir() and d.resolve() not in keep]


def _catalog_keys() -> set[str]:
    """The current variant Keys (1_variants_full.csv) — the live image targets."""
    f = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
    if not f.exists():
        return set()
    with f.open(encoding="utf-8-sig", newline="") as h:
        return {r["Key"] for r in csv.DictReader(h) if (r.get("Key") or "").strip()}


def _orphan_images(image_pipeline_root: Path, catalog_keys: set[str]) -> list[Path]:
    """image_pipeline/{images,to_upload}/<Key>.png whose Key is no longer in the catalog — a
    generated image for a variant that was removed or renamed. Returns nothing when catalog_keys
    is empty, so a missing/unbuilt catalog never treats every generated image as orphaned."""
    if not catalog_keys:
        return []
    orphans: list[Path] = []
    for sub in ("images", "to_upload"):
        d = image_pipeline_root / sub
        if d.exists():
            orphans += [p for p in d.glob("*.png") if p.stem not in catalog_keys]
    return orphans


def run(dry_run: bool = False, data_dir: Path | None = None, outputs: Path | None = None,
        image_pipeline_root: Path | None = None, catalog_keys: set[str] | None = None) -> dict[str, int]:
    data_dir = Path(data_dir or SETTINGS.paths.data_dir)
    outputs = Path(outputs or SETTINGS.paths.outputs_dir)
    image_pipeline_root = (Path(image_pipeline_root) if image_pipeline_root
                           else data_dir.parent / "image_pipeline")
    if catalog_keys is None:
        catalog_keys = _catalog_keys()

    dir_targets: dict[str, list[Path]] = {
        "superseded_scrapes": _superseded_scrapes(data_dir),
        "superseded_runs": _superseded_runs(outputs),
        "stale_inventory_runs": _superseded_runs(outputs / "_inventory"),
    }
    file_targets: dict[str, list[Path]] = {
        "orphan_images": _orphan_images(image_pipeline_root, catalog_keys),
        "ds_store": [p for base in (data_dir, outputs) if base.exists() for p in base.rglob(".DS_Store")],
    }

    counts: dict[str, int] = {}
    for label, paths in dir_targets.items():
        for p in paths:
            log.info(f"{'would remove' if dry_run else 'removing'} {label}", extra={"extra_fields": {"path": str(p)}})
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
        counts[label] = len(paths)
    for label, paths in file_targets.items():
        for p in paths:
            log.info(f"{'would remove' if dry_run else 'removing'} {label}", extra={"extra_fields": {"path": str(p)}})
            if not dry_run:
                p.unlink(missing_ok=True)
        counts[label] = len(paths)

    log.info("clean done" if not dry_run else "clean dry-run", extra={"extra_fields": counts})
    return counts


def delete_source_data(sources: list[str]) -> dict[str, int]:
    """Delete ALL raw scraped data for the given sources (data/<source>/*) -- a full wipe so the next
    scrape starts fresh. Returns {source: scrape_folders_removed}. Base config + ledger are untouched
    (the ledger has its own reset); only the raw scrape inputs go."""
    out: dict[str, int] = {}
    for s in sources:
        d = SETTINGS.paths.data_dir / s
        folders = [f for f in d.glob("*") if f.is_dir()] if d.exists() else []
        for f in folders:
            shutil.rmtree(f, ignore_errors=True)
        out[s] = len(folders)
        log.info("deleted scraped data", extra={"extra_fields": {"source": s, "removed": len(folders)}})
    return out


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv or "-n" in argv
    counts = run(dry_run=dry)
    verb = "would remove" if dry else "removed"
    print(f"clean ({'dry-run' if dry else 'done'}): {verb} "
          + ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
