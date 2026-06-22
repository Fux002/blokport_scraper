"""Emit the complete variant upload file -- one deterministic, isolated step.

The catalog is a pure function of two inputs that NEVER alias:
  - the immutable Medusa export (existing variants, download-only), and
  - this run's scraped additions (new variants + confirmed alias updates).

This stage assembles the full file the user uploads, so nothing is hand-merged or
stamped in place and re-running yields an identical file:

  existing export            ─┐
  1_variants_update.csv       ├─►  1_variants_full.csv    (existing ∪ new ∪ mirror,
  mirror backbones (tiles)   ─┘                            Image + Volume stamped)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config.settings import CATEGORIES, SETTINGS, category, category_for_key
from stone_pipeline.core import logfmt

log = logfmt.get_logger("emit_catalog")

_COLS = ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]


def _rows(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(encoding="utf-8-sig") as h:
        return [{c: (r.get(c) or "") for c in _COLS}
                for r in csv.DictReader(h) if (r.get("Key") or "").strip()]


def _mirror_rows(by_key: dict[str, dict]) -> list[dict]:
    """One variant row per source variety for each active mirror category (tiles
    mirror slabs). The mirror's deterministic Key carries the source variant's
    Name and Aliases, so a tile stays identical to its slab."""
    out: list[dict] = []
    for cat in CATEGORIES:
        if not (cat.mirror_of and cat.active):
            continue
        src = category(cat.mirror_of)
        src_posts = json.loads(src.backbone_path.read_text(encoding="utf-8-sig"))["posts"]
        mir_posts = json.loads(cat.backbone_path.read_text(encoding="utf-8-sig"))["posts"]
        for sp, mp in zip(src_posts, mir_posts):  # backbones are built in lockstep
            s = by_key.get(sp.get("key"))
            if s:
                out.append({"Key": mp["key"], "Name": s["Name"], "Image": "",
                            "Aliases": s["Aliases"], "Volume per kg (m³/kg)": ""})
    return out


def build(existing_path: Path | None = None) -> Path:
    """Assemble to_upload/1_variants_full.csv from the immutable export + this run's
    delta (to_upload/1_variants_update.csv)."""
    existing_path = Path(existing_path or SETTINGS.paths.export_file)
    by_key = {r["Key"]: r for r in _rows(existing_path)}      # existing (read-only)
    order = list(by_key)
    n_existing = len(order)
    for r in _rows(SETTINGS.paths.to_upload_dir / "1_variants_update.csv"):   # new + alias delta
        if r["Key"] in by_key:
            by_key[r["Key"]].update(r)                        # alias update onto existing row
        else:
            by_key[r["Key"]] = r
            order.append(r["Key"])                            # genuinely new variant
    mirror = [m for m in _mirror_rows(by_key) if m["Key"] not in by_key]  # tiles for existing varieties
    rows = [by_key[k] for k in order] + mirror

    base = SETTINGS.curation.variant_image_base               # dev/prod switch via env
    for r in rows:
        cat = category_for_key(r["Key"])
        r["Image"] = f"{base}{r['Key']}.png"
        r["Volume per kg (m³/kg)"] = cat.volume_per_kg if cat else ""

    path = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=_COLS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in _COLS} for r in rows)
    log.info("variants_full emitted", extra={"extra_fields": {
        "rows": len(rows), "existing": n_existing,
        "new": len(order) - n_existing, "mirror": len(mirror)}})
    return path
