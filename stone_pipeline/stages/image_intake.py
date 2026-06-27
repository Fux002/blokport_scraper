"""Image intake: take the variant images you already have (old names) and bring
them onto the {Key}.png convention, then report what still needs generating.

You drop your current images into  images/inbox/{slabs,blocks,tiles}/  using the
old naming ({branch}_rb_<id>_<type>_<name>.png). This:

  1. reads the variant files (which carry the unique Key),
  2. matches each dropped image to its variant and copies it to
     images/renamed/{Key}.png  (originals untouched), and
  3. writes reports:
       - intake_report.csv      every input file -> matched Key (and how)
       - unmatched_images.csv   inputs with no matching variant (orphans)
       - images_to_generate.csv variants that have NO image anywhere, with the
                                exact {Key}.png filename to generate.

Matching is by the variant's name STEM -- {branch}_<type>_<name>, i.e. the Key
without its trailing uuid -- which is robust to the old rb_<id> numbering and to
the export's extra -<uuid> suffix. Nothing is deleted; renamed images are copies.
"""

from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from pathlib import Path

from stone_pipeline.config.settings import CATEGORIES, SETTINGS, category_for_key
from stone_pipeline.core import logfmt

log = logfmt.get_logger("image_intake")

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
# inbox subfolder (category plural) -> branch prefix, from the registry
_INBOX_BRANCH = {c.plural: c.name for c in CATEGORIES}


def key_stem(key: str) -> str:
    """The Key without its trailing uuid: slab_agate_agata_black_<uuid> -> slab_agate_agata_black."""
    return re.sub(rf"_{_UUID}$", "", (key or "").strip().lower())


def file_stem(filename: str) -> str:
    """Reduce an old-style image filename to the same stem as a Key:
    slab_rb_318_agate_agata_black.png        -> slab_agate_agata_black
    variations/block_rb_5_marble_x-<uuid>.png -> block_marble_x"""
    name = Path(filename).stem.lower()
    name = re.sub(r"_rb_\d+_", "_", name, count=1)     # drop the legacy rb_<id>
    name = re.sub(rf"[-_]{_UUID}$", "", name)          # drop a trailing -<uuid>/_<uuid>
    name = re.sub(r"_crop_\d+x\d+$", "", name)         # drop a thumbnail suffix
    return name.strip("_-")


def _variant_files() -> list[Path]:
    """The existing variants to match dropped inbox images against: the combined
    cross-category export (Key + Medusa Id), like Medusa's single variants page.
    The category is the Key prefix."""
    p = SETTINGS.paths
    return [p.export_file] if p.export_file.exists() else []


def build_lookups():
    """Return (by_stem, by_basename, variants) over all variant files.
    by_stem: stem -> set(Key); by_basename: image basename -> Key;
    variants: Key -> {"Name","branch","has_image"}."""
    by_stem: dict[str, set] = defaultdict(set)
    by_basename: dict[str, str] = {}
    variants: dict[str, dict] = {}
    for f in _variant_files():
        with f.open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                key = (r.get("Key") or "").strip()
                if not key:
                    continue
                cat = category_for_key(key)        # None for a malformed/legacy Key prefix
                if cat is None:
                    continue                        # skip that row, don't crash the whole intake
                img = (r.get("Image") or "").strip()
                rec = variants.setdefault(key, {"Name": (r.get("Name") or "").strip(),
                                                "branch": cat.name, "has_image": False})
                by_stem[key_stem(key)].add(key)
                if img:
                    by_basename[Path(img).name.lower()] = key
                    rec["has_image"] = True
    return by_stem, by_basename, variants


def match_image(filename: str, by_stem, by_basename) -> tuple[str | None, str]:
    """Return (Key, method). method is exact|stem|ambiguous:<n>|no_variant."""
    base = Path(filename).name.lower()
    if base in by_basename:
        return by_basename[base], "exact"
    keys = by_stem.get(file_stem(filename)) or set()
    if len(keys) == 1:
        return next(iter(keys)), "stem"
    if len(keys) > 1:
        return None, f"ambiguous:{len(keys)}"
    return None, "no_variant"


def run(inbox_root: Path | None = None, output_dir: Path | None = None,
        reports_dir: Path | None = None) -> dict:
    images_root = SETTINGS.paths.workspace_root / "images"  # project root
    inbox_root = Path(inbox_root or images_root / "inbox")
    output_dir = Path(output_dir or images_root / "renamed")
    reports_dir = Path(reports_dir or images_root / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    by_stem, by_basename, variants = build_lookups()
    report: list[dict] = []
    matched_keys: set[str] = set()

    for sub, branch in _INBOX_BRANCH.items():
        d = inbox_root / sub
        if not d.exists():
            continue
        for img in sorted(d.iterdir()):
            if not img.is_file() or img.suffix.lower() not in _IMG_EXT:
                continue
            key, method = match_image(img.name, by_stem, by_basename)
            mismatch = bool(key) and not key.startswith(branch + "_")
            if key and not mismatch:
                shutil.copy2(img, output_dir / f"{key}.png")
                matched_keys.add(key)
            report.append({
                "inbox": sub, "input": img.name,
                "matched_key": key or "", "new_image": f"{key}.png" if key and not mismatch else "",
                "method": "branch_mismatch" if mismatch else method,
            })

    # variants that have NO image anywhere: no inbox match AND no Image in the file
    to_generate = [
        {"Key": k, "Name": v["Name"], "branch": v["branch"], "image_to_generate": f"{k}.png"}
        for k, v in sorted(variants.items())
        if k not in matched_keys and not v["has_image"]
    ]
    unmatched = [r for r in report if not r["new_image"]]

    _write(reports_dir / "intake_report.csv",
           ["inbox", "input", "matched_key", "new_image", "method"], report)
    _write(reports_dir / "unmatched_images.csv",
           ["inbox", "input", "matched_key", "new_image", "method"], unmatched)
    _write(reports_dir / "images_to_generate.csv",
           ["Key", "Name", "branch", "image_to_generate"], to_generate)

    summary = {"renamed": len(matched_keys), "inputs": len(report),
               "unmatched": len(unmatched), "to_generate": len(to_generate),
               "variants_total": len(variants)}
    log.info("image intake done", extra={"extra_fields": summary})
    return {**summary, "output_dir": str(output_dir), "reports_dir": str(reports_dir)}


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
