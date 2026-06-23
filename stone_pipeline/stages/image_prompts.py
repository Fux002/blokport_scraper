"""Build the fal.ai generator's prompts file for variants that need an image.

One module, three modes -- all write the SAME file (image_pipeline/prompts_to_generate.json,
the only file the generator reads):

  build()               new product-backed variants only   (normal pipeline / no args)
  build_regeneration()  every variant that has an image     (`--regenerate`: replace old-model set)
  build_for_keys(keys)  a specific set of variant Keys      (`--keys K1 K2 …`: fix a few bad images)

`output_name` IS the variant Key, so the generator writes {Key}.png and the S3 upload OVERWRITES
that one object -- one image name per variant, replaced in place, never a new name. The generator
appends a natural-stone guard so a variety named after a painting/fruit (e.g. 'Mona Lisa') renders
stone, not the subject.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS, category_for_key
from stone_pipeline.core import logfmt

log = logfmt.get_logger("image_prompts")

# surface directives kept by the generator (composition-breakers are dropped there)
_DIRECTIVES = ("No shadows, no light glare, no reflections, no highlights, "
               "flat even lighting, no text, no watermark, straight-on flat view, "
               "no background, no perspective.")


def _prompt_item(key: str, stone_type: str, name: str) -> dict | None:
    """One generator prompt for a variant Key, or None if its category has no texture base
    (e.g. accessories use real product photos, not generation)."""
    cat = category_for_key(key)
    if not cat or not cat.base_image:
        return None
    into = f"{stone_type}: {name}" if stone_type else name
    return {"output_name": key,                       # generator writes {key}.png -> overwrite in place
            "base_image_url": cat.base_image,         # slab base for slabs, block base for blocks
            "prompt": f"Transform this attached image into {into}. {_DIRECTIVES}"}


def _write(items: list[dict], out_path: Path | None, mode: str) -> Path:
    out = Path(out_path or SETTINGS.paths.workspace_root / "image_pipeline" / "prompts_to_generate.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("image prompts built", extra={"extra_fields": {"mode": mode, "prompts": len(items), "out": str(out)}})
    return out


def _backbone_types() -> dict[str, str]:
    """Key -> stone_type from every backbone (full + new-variety additions)."""
    ktype: dict[str, str] = {}
    cs = SETTINGS.paths.catalog_source_dir
    for f in sorted(cs.glob("backbone_*.json")):
        data = json.loads(f.read_text(encoding="utf-8-sig"))
        for p in (data.get("posts") if isinstance(data, dict) else data):
            if p.get("key"):
                ktype[p["key"]] = (p.get("stone_type") or "").strip()
    for f in sorted((cs / "backbone_additions").glob("*.json")):
        for p in json.loads(f.read_text(encoding="utf-8")):
            if p.get("key"):
                ktype.setdefault(p["key"], (p.get("stone_type") or "").strip())
    return ktype


def _variants() -> dict[str, dict]:
    """Key -> row from 1_variants_full.csv (for Name + whether it has an Image)."""
    out: dict[str, dict] = {}
    f = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
    if f.exists():
        with f.open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                if (r.get("Key") or "").strip():
                    out[r["Key"].strip()] = r
    return out


def build(additions_dir: Path | None = None, out_path: Path | None = None) -> Path:
    """New product-backed variants only -- the normal pipeline step. Reads the new-variety
    backbone deltas; fan-out copies with no product are skipped (no wasted generation)."""
    additions_dir = Path(additions_dir or SETTINGS.paths.catalog_source_dir / "backbone_additions")
    items, seen = [], set()
    for f in sorted(additions_dir.glob("*.json")):
        for p in json.loads(f.read_text(encoding="utf-8")):
            key = (p.get("key") or "").strip()
            if not key or key in seen or not p.get("product_backed", False):
                continue
            item = _prompt_item(key, (p.get("stone_type") or "").strip(), (p.get("variant") or "").strip())
            if item:
                seen.add(key)
                items.append(item)
    return _write(items, out_path, "new")


def build_regeneration(out_path: Path | None = None) -> Path:
    """Every variant that has an image -- re-make the old, inferior-model set (and first-make the
    product-backed new ones) with the current model. Imageless mirror tiles/blocks are skipped.
    To force a true REGEN, clear image_pipeline/images/ first (the generator skips files it finds)."""
    ktype = _backbone_types()
    items = [item for key, r in _variants().items()
             if (r.get("Image") or "").strip()
             and (item := _prompt_item(key, ktype.get(key, ""), (r.get("Name") or "").strip()))]
    return _write(items, out_path, "regenerate")


def build_for_keys(keys: list[str], out_path: Path | None = None) -> Path:
    """A specific set of variant Keys -- regenerate just these (e.g. replace a few bad images)."""
    ktype, variants = _backbone_types(), _variants()
    items = []
    for key in (k.strip() for k in keys):
        item = _prompt_item(key, ktype.get(key, ""), (variants.get(key, {}).get("Name") or "").strip())
        if item:
            items.append(item)
        else:
            log.warning("skipped key (no base image / unknown)", extra={"extra_fields": {"key": key}})
    return _write(items, out_path, "keys")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args[:1] == ["--regenerate"]:
        p = build_regeneration()
    elif args[:1] == ["--keys"]:
        p = build_for_keys(args[1:])
    else:
        p = build()
    print(f"wrote {p}")
