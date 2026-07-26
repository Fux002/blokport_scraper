"""Build the fal.ai generator's prompts file for variants that need an image.

One module, three modes -- all write the SAME file (image_pipeline/prompts_to_generate.json,
the only file the generator reads):

  build()               new product-backed variants only   (normal pipeline / no args)
  build_refresh(done)   product-backed images not yet re-made with the best model  (one-time quality pass)
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


def product_backed_keys() -> set[str]:
    """Variant Keys a product references (export Key<->Id map + the products' variation ids). A
    product-backed variant MUST have an image -- including an EXISTING fan-out tile/block that
    gained a product AFTER its slab was first created (those are never in backbone_additions, so
    the new-variety queue alone misses them)."""
    keys: set[str] = set()
    exp = SETTINGS.paths.export_file
    prods = SETTINGS.paths.to_upload_dir / "3_products_all.csv"
    if not (exp.exists() and prods.exists()):
        return keys
    id_to_key: dict[str, str] = {}
    with exp.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            if (r.get("Id") or "").strip() and (r.get("Key") or "").strip():
                id_to_key[r["Id"].strip()] = r["Key"].strip()
    with prods.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            k = id_to_key.get((r.get("STN Variation Id") or "").strip())
            if k:
                keys.add(k)
    return keys


def build(additions_dir: Path | None = None, out_path: Path | None = None) -> Path:
    """Product-backed variants needing an image. Two sources: (1) the new-variety backbone deltas
    (fan-out copies with no product are skipped), and (2) BACKFILL -- existing variants that have a
    product but still no image (e.g. a tile/block that gained a product after its slab was created;
    these are never in backbone_additions, so without this they'd never be generated)."""
    run_backfill = additions_dir is None         # backfill is whole-catalog; skip on a scoped call
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
    # (2) BACKFILL: every category variant of a STOCKED variety that has NO image yet. A variety is stocked
    # when ANY of its category keys is product-backed; then EACH of its slab/block/tile variants needs its
    # OWN image, generated from that category's base image (a tile is not a copy of the slab -- different
    # base, different render). This is why a stocked variety's tile/block was blank: it is not itself
    # product-backed (the product references the slab), so keying the backfill on the product-backed KEY
    # missed it. Signal is the immutable EXPORT Image (blank), NOT 1_variants_full -- emit may have just
    # stamped the CSV link, but the real S3 image isn't generated until this queue runs; once generated +
    # uploaded + re-exported, the export Image is non-blank and it drops out of the queue.
    backed, ktype = (product_backed_keys(), _backbone_types()) if run_backfill else (set(), {})
    exp = SETTINGS.paths.export_file
    if run_backfill and exp.exists():
        with exp.open(encoding="utf-8-sig", newline="") as h:
            rows = list(csv.DictReader(h))

        def _ident(r) -> tuple[str, str]:
            k = (r.get("Key") or "").strip()
            return (ktype.get(k, ""), (r.get("Name") or "").strip())   # (stone_type, variant): the variety

        stocked = {_ident(r) for r in rows if (r.get("Key") or "").strip() in backed}
        for r in rows:
            key = (r.get("Key") or "").strip()
            if not key or key in seen or _ident(r) not in stocked or (r.get("Image") or "").strip():
                continue
            item = _prompt_item(key, ktype.get(key, ""), (r.get("Name") or "").strip())
            if item:
                seen.add(key)
                items.append(item)
    return _write(items, out_path, "new")


def build_refresh(refreshed: set[str], out_path: Path | None = None) -> Path:
    """Product-backed variants whose {Key}.png was made with an OLDER model, to re-make ONCE with the
    current best model -- a one-time QUALITY pass, not the new-image queue. A variant qualifies only if it
    ALREADY has an image AND backs a product (a variant with no product does not need a fresh texture; an
    imageless one is build()'s job). `refreshed` is the durable set of Keys already re-made with the best
    model, so a re-run never regenerates -- and re-charges -- the same image twice. Driven by
    refresh_images, which owns that marker and the cost gate."""
    backed, ktype, variants = product_backed_keys(), _backbone_types(), _variants()
    items = []
    for key in sorted(backed):
        r = variants.get(key)
        if not r or not (r.get("Image") or "").strip() or key in refreshed:
            continue
        item = _prompt_item(key, ktype.get(key, ""), (r.get("Name") or "").strip())
        if item:
            items.append(item)
    return _write(items, out_path, "refresh")


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
    # the one-time quality refresh (build_refresh) is driven by `python -m stone_pipeline.refresh_images`,
    # which owns the durable "already refreshed" marker + the cost gate, so it is not a raw prompt mode here.
    if args[:1] == ["--keys"]:
        p = build_for_keys(args[1:])
    else:
        p = build()
    print(f"wrote {p}")
