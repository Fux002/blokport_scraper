"""variants_export_base.csv = the full variant list. There is ONE list, not three:

  * 1_variants_full.csv      the complete, gated, S3-imaged, Id-free variant set built each run
  * variants_export_base.csv THE SAME set, persisted as the committed seed of truth (base == full)
  * 1_variants_update.csv     just the NEW slice of that set (a subset of full), for incremental load
  * variants_export.csv       the live Medusa state WITH ids (matching + round-trip)

So base is simply a copy of the freshly-built 1_variants_full: load full and you have loaded base;
the update is the part of it Medusa doesn't have yet. This runs at the END of catalog.run, after the
image-gate has finalised full, so the seed always equals the complete current catalog -- never a
second, diverging list. Re-runnable standalone:

    python -m stone_pipeline.reference.sync_variants_base
"""

from __future__ import annotations

import csv
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("sync_variants_base")

# FND-04: derive the env folder like every other from_medusa file (from_medusa/<env>/), never a
# hardcoded 'development' -- else a production run reads/writes this committed source-of-truth seed under
# the dev folder. from_medusa_dir follows ENV_NAME, so dev and prod each use their own base.
BASE = SETTINGS.paths.from_medusa_dir / "variants_export_base.csv"
FULL = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
# base carries exactly the upload columns and NO Medusa Id (Medusa mints ids on import)
COLS = ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]


def sync(full_path: Path = FULL, base_path: Path = BASE, write: bool = True) -> dict:
    """base := 1_variants_full (the one complete list). No-op if full hasn't been built yet."""
    full_path, base_path = Path(full_path), Path(base_path)
    if not full_path.exists():
        log.warning("no 1_variants_full to seed base from; leaving base unchanged")
        return {"base_rows": 0, "skipped": True}
    rows = [{c: (r.get(c) or "") for c in COLS}
            for r in csv.DictReader(full_path.open(encoding="utf-8-sig"))]
    if write:
        with base_path.open("w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=COLS)
            w.writeheader()
            w.writerows(rows)
    stats = {"base_rows": len(rows), "skipped": False}
    log.info("base seed updated (base == 1_variants_full)", extra={"extra_fields": stats})
    return stats


if __name__ == "__main__":
    s = sync()
    print(f"base = 1_variants_full: {s['base_rows']} variants" if not s["skipped"]
          else "skipped: 1_variants_full not built yet")
