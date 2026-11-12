"""Image intake: take the variety icons you already have (old names) and bring them onto the icon-pair convention
`{Key}/{category name}.png` (spec rev 8 section 4), then report what still needs generating.

You drop your current images into  images/inbox/{slabs,blocks,tiles}/  (the folder names the category) using the old
naming ({branch}_rb_<id>_<type>_<name>.png) or the variety's Key. This:

  1. reads the variety export (which carries the unique Key),
  2. matches each dropped image to its variety and copies it to
     images/renamed/{Key}/{category}.png  (originals untouched), and
  3. writes reports:
       - intake_report.csv      every input file -> matched Key (and how)
       - unmatched_images.csv   inputs with no matching variety (orphans)
       - images_to_generate.csv the product-backed icon pairs with no icon on S3 and none dropped here.

Matching is by the variety's name STEM -- {type}_{name}, the Key without its trailing uuid and with its one type
separator folded -- which is robust to the old rb_<id> numbering, the old {branch}_ prefix and the export's extra
-<uuid> suffix. Nothing is deleted; renamed images are copies.
"""

from __future__ import annotations

import csv
import re
import shutil
from collections import defaultdict
from pathlib import Path

from stone_pipeline.config.categories import CATEGORIES
from stone_pipeline.config.settings import SETTINGS, VARIANTS_EXPORT_STATUS_REQUIRED
from stone_pipeline.core import logfmt
from stone_pipeline.core.text import ascii_fold
from stone_pipeline.io import variety_icons
from stone_pipeline.phase1.variety_key import SEP
from stone_pipeline.reference.medusa_files import read_variants_export
from stone_pipeline.stages.image_prompts import _existing_icons, product_backed_pairs

log = logfmt.get_logger("image_intake")

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
# inbox subfolder (category plural) -> the category name, from the registry
_INBOX_CATEGORY = {c.plural: c.name for c in CATEGORIES}


def key_stem(key: str) -> str:
    """A Key without its trailing uuid, its type separator folded: granite__agata_black_<uuid> -> granite_agata_black."""
    return re.sub(rf"_{_UUID}$", "", ascii_fold((key or "").strip()).lower()).replace(SEP, "_")


def file_stem(filename: str, category_name: str) -> str:
    """Reduce an inbox image filename to the same stem as a Key:
    block_rb_318_agate_agata_black.png       -> agate_agata_black   (in inbox/blocks)
    block_rb_5_marble_x-<uuid>.png           -> marble_x"""
    name = ascii_fold(Path(filename).stem).lower()   # fold accents so an accented inbox filename matches the ASCII Key stem
    name = re.sub(r"_rb_\d+_", "_", name, count=1)     # drop the legacy rb_<id>
    name = re.sub(rf"[-_]{_UUID}$", "", name)          # drop a trailing -<uuid>/_<uuid>
    name = re.sub(r"_crop_\d+x\d+$", "", name)         # drop a thumbnail suffix
    name = name.strip("_-")
    return name.removeprefix(f"{category_name}_")     # the old {branch}_ prefix: the folder names the category


def _variant_files() -> list[Path]:
    """The existing varieties to match dropped inbox images against: the variety export (Key + Medusa Id)."""
    p = SETTINGS.paths
    return [p.export_file] if p.export_file.exists() else []


def build_lookups():
    """Return (by_stem, by_key, names) over the variety export.
    by_stem: stem -> set(Key); by_key: lower-cased Key -> Key; names: Key -> Name."""
    by_stem: dict[str, set] = defaultdict(set)
    by_key: dict[str, str] = {}
    names: dict[str, str] = {}
    for f in _variant_files():
        for r in read_variants_export(f, require_status=VARIANTS_EXPORT_STATUS_REQUIRED):
            if not r.key:
                continue
            names.setdefault(r.key, r.name)
            by_stem[key_stem(r.key)].add(r.key)
            by_key[r.key.lower()] = r.key
    return by_stem, by_key, names


def match_image(filename: str, category_name: str, by_stem, by_key) -> tuple[str | None, str]:
    """Return (Key, method). method is exact|stem|ambiguous:<n>|no_variant."""
    exact = by_key.get(Path(filename).stem.lower())
    if exact:
        return exact, "exact"
    keys = by_stem.get(file_stem(filename, category_name)) or set()
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

    by_stem, by_key, names = build_lookups()
    report: list[dict] = []
    matched: set[str] = set()                 # pair ids brought in

    for sub, category_name in _INBOX_CATEGORY.items():
        d = inbox_root / sub
        if not d.exists():
            continue
        for img in sorted(d.iterdir()):
            if not img.is_file() or img.suffix.lower() not in _IMG_EXT:
                continue
            key, method = match_image(img.name, category_name, by_stem, by_key)
            pid = variety_icons.pair_id(key, category_name) if key else ""
            if pid:
                target = output_dir / f"{pid}.png"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(img, target)
                matched.add(pid)
            report.append({"inbox": sub, "input": img.name, "matched_key": key or "",
                           "new_image": f"{pid}.png" if pid else "", "method": method})

    # the product-backed pairs with no icon: none on S3 (unknown off production: none proven) and none dropped here
    icons = _existing_icons() or set()
    to_generate = [{"Key": key, "Name": names.get(key, ""), "category": name,
                    "icon": f"{variety_icons.pair_id(key, name)}.png"}
                   for key, name in sorted(product_backed_pairs())
                   if variety_icons.pair_id(key, name) not in icons | matched]
    unmatched = [r for r in report if not r["new_image"]]

    _write(reports_dir / "intake_report.csv",
           ["inbox", "input", "matched_key", "new_image", "method"], report)
    _write(reports_dir / "unmatched_images.csv",
           ["inbox", "input", "matched_key", "new_image", "method"], unmatched)
    _write(reports_dir / "images_to_generate.csv", ["Key", "Name", "category", "icon"], to_generate)

    summary = {"renamed": len(matched), "inputs": len(report),
               "unmatched": len(unmatched), "to_generate": len(to_generate),
               "variants_total": len(names)}
    log.info("image intake done", extra={"extra_fields": summary})
    return {**summary, "output_dir": str(output_dir), "reports_dir": str(reports_dir)}


def _write(path: Path, cols: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
