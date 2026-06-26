"""Keep variants_export_base.csv -- the SOURCE OF TRUTH -- clean and correct.

variants_export_base.csv seeds a from-zero import on the dev server and is the canonical list of every
variant we have built up. Two hard rules define it:

  1. It does NOT hold the Medusa Id. The Id is minted by Medusa on import; the seed must be Id-free or
     a re-import would fight the server over ids. Columns: Key, Name, Image, Aliases, Volume per kg.
  2. Its Image column holds the REAL S3 link for the variant ({base}/{Key}.png) -- and only when that
     {Key}.png actually exists on S3, so the seed never points at a missing image.

This unions the live variants_export.csv INTO base (so variants/aliases completed on the live server
accumulate and are never lost), DROPS the Id, and (re)stamps every Image from the current S3 state.
Nothing is ever removed here -- base only grows; junk removal is the separate variants_to_delete flow.

    python -m stone_pipeline.reference.sync_variants_base
"""

from __future__ import annotations

import csv
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("sync_variants_base")

_DEV = SETTINGS.paths.workspace_root / "from_medusa" / "development"
BASE = _DEV / "variants_export_base.csv"
EXPORT = _DEV / "variants_export.csv"
ALIAS_COL = "Aliases"


def _delete_keys() -> set[str]:
    """Keys flagged for deletion in Medusa (variants_to_delete) -- excluded from base so the seed of
    truth never carries junk/mis-typed variants that are on their way out."""
    path = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    if not path.exists():
        return set()
    import csv as _csv
    return {(r.get("Key") or "").strip()
            for r in _csv.DictReader(path.open(encoding="utf-8-sig")) if (r.get("Key") or "").strip()}
# the Id is deliberately absent -- base is the Id-free seed of truth
OUT_FIELDS = ["Key", "Name", "Image", ALIAS_COL, "Volume per kg (m³/kg)"]


def _aliases(s: str) -> list[str]:
    return [a.strip() for a in (s or "").split("|") if a.strip()]


def _union_aliases(a: str, b: str) -> str:
    out, seen = [], set()
    for alias in _aliases(a) + _aliases(b):
        key = alias.casefold()
        if key not in seen:
            seen.add(key)
            out.append(alias)
    return "|".join(out)


def _s3_variation_keys():
    """Set of Keys with a {Key}.png on S3, or None if S3 is unreachable (then Images are left as-is
    rather than wiped). Reuses emit_catalog's single list call."""
    try:
        from stone_pipeline.stages.emit_catalog import _s3_variation_keys as fetch
        return fetch()
    except Exception as exc:  # noqa: BLE001 -- any S3/import failure must not break the seed sync
        log.warning("S3 variation list unavailable; leaving base Images unchanged",
                    extra={"extra_fields": {"error": repr(exc)[:120]}})
        return None


def sync(base_path: Path = BASE, export_path: Path = EXPORT, write: bool = True,
         s3_keys=None, image_base: str | None = None) -> dict:
    base_rows = list(csv.DictReader(base_path.open(encoding="utf-8-sig"))) if base_path.exists() else []
    export_rows = list(csv.DictReader(export_path.open(encoding="utf-8-sig"))) if export_path.exists() else []
    image_base = image_base if image_base is not None else SETTINGS.curation.variant_image_base
    if s3_keys is None:
        s3_keys = _s3_variation_keys()

    # union by Key, base first (its order/values win), then any export-only variant appended. A blank
    # Key is not an identity -- skip it rather than collapse every blank-Key row into one.
    delete_keys = _delete_keys()
    base_keys = {(r.get("Key") or "").strip() for r in base_rows}
    by_key: dict[str, dict] = {}
    order: list[str] = []
    skipped_blank = aliases_added = added = dropped_deleted = 0
    for r in base_rows + export_rows:
        k = (r.get("Key") or "").strip()
        if not k:
            skipped_blank += 1
            continue
        if k in delete_keys:               # flagged for deletion -> keep it OUT of the seed of truth
            dropped_deleted += 1
            continue
        if k not in by_key:
            by_key[k] = {f: (r.get(f) or "") for f in OUT_FIELDS}
            by_key[k]["Key"] = k
            order.append(k)
            if k not in base_keys:
                added += 1
            continue
        cur = by_key[k]
        merged = _union_aliases(cur.get(ALIAS_COL, ""), r.get(ALIAS_COL, ""))
        if merged != cur.get(ALIAS_COL, ""):
            aliases_added += len(_aliases(merged)) - len(_aliases(cur.get(ALIAS_COL, "")))
            cur[ALIAS_COL] = merged
        for f in ("Name", "Volume per kg (m³/kg)"):     # backfill an empty base field from export
            if not (cur.get(f) or "").strip() and (r.get(f) or "").strip():
                cur[f] = r[f]

    # (re)stamp Image from the live S3 state: the full {base}{Key}.png link iff the image exists.
    imaged = 0
    if s3_keys is not None:
        for k, row in by_key.items():
            row["Image"] = f"{image_base}{k}.png" if k in s3_keys else ""
            if row["Image"]:
                imaged += 1

    rows = [by_key[k] for k in order]
    if write:
        with base_path.open("w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=OUT_FIELDS)
            w.writeheader()
            w.writerows(rows)
    stats = {"base_rows": len(rows), "variants_added": added, "aliases_added": aliases_added,
             "images_linked": imaged, "skipped_blank_key": skipped_blank,
             "dropped_flagged_for_deletion": dropped_deleted, "s3_checked": s3_keys is not None}
    log.info("variants base synced (Id-free, S3-linked)", extra={"extra_fields": stats})
    return stats


if __name__ == "__main__":
    s = sync()
    print(f"base synced: {s['base_rows']} variants (Id-free), +{s['variants_added']} new, "
          f"{s['images_linked']} S3 image links" + ("" if s["s3_checked"] else " [S3 unreachable: Images kept]"))
