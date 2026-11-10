"""Build the fal.ai generator's prompts file for the variety icons that need a texture.

One module, three modes -- all write the SAME file (image_pipeline/prompts_to_generate.json,
the only file the generator reads):

  build()                         new product-backed icon pairs only   (normal pipeline / no args)
  build_with_upgrades(done, cap)  the produce's queue: the new ones plus up to `cap` older textures re-made
  build_for_keys(pair_ids)        a specific set of icon pairs        (`--keys K/slab K/block`: fix a few bad icons)

A texture is ONE (Key, category) pair (spec rev 8 section 4): a queue item carries `key` and `category`, and is named by
its pair id `{Key}/{category name}` (io.variety_icons.pair_id), so the generator writes `{Key}/{category}.png` and the
upload OVERWRITES that one icon in place, never a new name. The generator appends a natural-stone guard so a variety
named after a painting/fruit (e.g. 'Mona Lisa') renders stone, not the subject.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from stone_pipeline.config.categories import CATEGORIES, category
from stone_pipeline.config.settings import SETTINGS, VARIANTS_EXPORT_STATUS_REQUIRED
from stone_pipeline.core import logfmt
from stone_pipeline.io import variety_icons
from stone_pipeline.reference.loaders.backbone import read_posts
from stone_pipeline.reference.medusa_files import read_variants_export

log = logfmt.get_logger("image_prompts")

# surface directives kept by the generator (composition-breakers are dropped there)
_DIRECTIVES = ("No shadows, no light glare, no reflections, no highlights, "
               "flat even lighting, no text, no watermark, straight-on flat view, "
               "no background, no perspective.")


def _prompt_item(key: str, category_name: str, stone_type: str, name: str) -> dict | None:
    """One generator prompt for an icon pair, or None if its category has no texture base (e.g. accessories use
    real product photos, not generation). The pair is checked by variety_icons (a Key the parser refuses, or a name
    that is not a pack category, raises)."""
    pid = variety_icons.pair_id(key, category_name)
    cat = category(category_name)
    if not cat.base_image:
        return None
    into = f"{stone_type}: {name}" if stone_type else name
    return {"output_name": pid, "key": key, "category": cat.name,   # the generator writes {Key}/{category}.png
            "base_image_url": cat.base_image,                       # the category's own base photo
            "prompt": f"Transform this attached image into {into}. {_DIRECTIVES}"}


def _write(items: list[dict], out_path: Path | None, mode: str) -> Path:
    out = Path(out_path or SETTINGS.paths.workspace_root / "image_pipeline" / "prompts_to_generate.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("image prompts built", extra={"extra_fields": {"mode": mode, "prompts": len(items), "out": str(out)}})
    return out


def _backbone_types() -> dict[str, str]:
    """Key -> stone_type from the variety backbone and this run's additions (the one post reader skips a held,
    key-less addition)."""
    ktype: dict[str, str] = {}
    for f in (SETTINGS.paths.backbone_json, SETTINGS.paths.backbone_additions_file):
        if f.exists():
            for p in read_posts(f):
                if p.get("key"):
                    ktype.setdefault(p["key"], (p.get("stone_type") or "").strip())
    return ktype


def _variants() -> dict[str, dict]:
    """Key -> row from 1_variants_full.csv (for the Name)."""
    out: dict[str, dict] = {}
    f = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
    if f.exists():
        with f.open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                if (r.get("Key") or "").strip():
                    out[r["Key"].strip()] = r
    return out


def _existing_icons() -> set[str] | None:
    """The pair ids of every icon on S3 (emit's one listing), or None when S3 cannot be listed (outside production;
    in production the listing raises)."""
    from stone_pipeline.stages.emit_catalog import _s3_icon_pairs
    pairs = _s3_icon_pairs()
    if pairs is None:
        return None
    return {variety_icons.pair_id(key, name) for key, names in pairs.items() for name in names}


def _export_file() -> Path:
    return SETTINGS.paths.export_file


def _products_file() -> Path:
    return SETTINGS.paths.to_upload_dir / "3_products_all.csv"


def _category_of_pcat() -> dict[str, str]:
    """The products file's `Product Category 1` (a category's Medusa pcat id) -> the pack category name."""
    return {c.pcat_id: c.name for c in CATEGORIES if c.pcat_id}


def product_backed_pairs() -> set[tuple[str, str]]:
    """(Key, category name) of every pair a product lists (spec section 4, N3): the product's variation id joined to
    the export's Key, and its `Product Category 1`. A product-backed pair MUST have an icon, including an EXISTING
    variety that gained a product in a new category (never in the additions, so the new-variety queue alone misses
    it). A category its products are not sold in gets no icon."""
    pairs: set[tuple[str, str]] = set()
    exp, prods = _export_file(), _products_file()
    if not (exp.exists() and prods.exists()):
        return pairs
    id_to_key = {r.id: r.key for r in read_variants_export(exp, require_status=VARIANTS_EXPORT_STATUS_REQUIRED)
                 if r.id and r.key}
    category_of = _category_of_pcat()
    with prods.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            key = id_to_key.get((r.get("STN Variation Id") or "").strip())
            name = category_of.get((r.get("Product Category 1") or "").strip())
            if key and name:
                pairs.add((key, name))
    return pairs


def build(additions_file: Path | None = None, out_path: Path | None = None) -> Path:
    """The icon pairs needing a texture, those without an icon on S3:
    - each category a new variety's additions post lists (the categories its scraped products sit in; a held,
      key-less post is never queued);
    - each product-backed pair of an existing variety.
    A category no product is sold in stays imageless (a record in Medusa, placeholdered by Blokport) until its own
    product appears. When S3 cannot be listed (outside production) nothing is proven present, so every pair is queued
    and the job's prune (prepare_variant_images) drops what exists."""
    additions_file = Path(additions_file or SETTINGS.paths.backbone_additions_file)
    icons = _existing_icons() or set()
    items, seen = [], set()

    def add(key: str, name: str, stone_type: str, variant: str) -> None:
        pid = variety_icons.pair_id(key, name)
        if pid in seen or pid in icons:
            return
        if item := _prompt_item(key, name, stone_type, variant):
            seen.add(pid)
            items.append(item)

    if additions_file.exists():
        for p in read_posts(additions_file):
            for name in p.get("categories") or []:
                add(p["key"], name, (p.get("stone_type") or "").strip(), (p.get("variant") or "").strip())
    backed = product_backed_pairs()
    if backed:
        variants, ktype = _variants(), _backbone_types()
        for key, name in sorted(backed):
            if (row := variants.get(key)) is not None:
                add(key, name, ktype.get(key, ""), (row.get("Name") or "").strip())
    return _write(items, out_path, "new")


UPGRADE_MODE = "upgrade"          # queue-item marker: an EXISTING texture being re-made, not a new mint


def upgrade_candidates(refreshed: set[str], cap: int, icons: set[str] | None) -> list[str]:
    """Product-backed pairs whose icon EXISTS (`icons`, the pair ids on S3) but was NOT made by the current best model,
    capped to `cap` for this run, by pair id so the drip is deterministic. Demand-driven: only a pair a product lists
    is upgraded. `refreshed` is the durable already-upgraded set, so each pair is upgraded exactly ONCE. S3 unknown
    (`icons` None) proves no icon exists, so nothing is upgraded. cap <= 0 (the default) disables upgrades."""
    if cap <= 0 or icons is None:
        return []
    eligible = [pid for pid in sorted(variety_icons.pair_id(k, n) for k, n in product_backed_pairs())
                if pid not in refreshed and pid in icons]
    return eligible[:cap]


def build_with_upgrades(refreshed: set[str], cap: int, out_path: Path | None = None) -> Path:
    """The produce's queue: new-mint prompts (build()) PLUS up to `cap` legacy-texture upgrades. Upgrade items carry
    mode=UPGRADE_MODE so the already-on-S3 prune keeps them (their icon exists by definition, which is exactly what
    the mint-idempotency prune drops). One queue, so the texture job handles both kinds unchanged."""
    build(out_path=out_path)
    path = Path(out_path or SETTINGS.paths.workspace_root / "image_pipeline" / "prompts_to_generate.json")
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    minted = {i.get("output_name") for i in items}
    ktype, variants = _backbone_types(), _variants()
    added = 0
    for pid in upgrade_candidates(refreshed, cap, _existing_icons()):
        if pid in minted:                      # a mint this run already covers it; never queue twice
            continue
        key, cat = variety_icons.pair_of(pid)
        item = _prompt_item(key, cat.name, ktype.get(key, ""), (variants.get(key, {}).get("Name") or "").strip())
        if item:
            item["mode"] = UPGRADE_MODE
            items.append(item)
            added += 1
    log.info("texture upgrades queued", extra={"extra_fields": {
        "mints": len(minted), "upgrades": added, "cap": cap}})
    return _write(items, out_path, "new+upgrade")   # _write logs the final counts


def build_for_keys(pair_ids: list[str], out_path: Path | None = None) -> Path:
    """A specific set of icon pairs (`{Key}/{category name}`) to regenerate, e.g. a few bad icons. A bare Key is
    refused: an icon is per category, so the category must be named."""
    ktype, variants = _backbone_types(), _variants()
    items = []
    for pid in (p.strip() for p in pair_ids):
        try:
            key, cat = variety_icons.pair_of(pid)
        except ValueError as exc:
            raise ValueError(f"build_for_keys takes icon pair ids ({{Key}}/{{category}}): {exc}") from exc
        item = _prompt_item(key, cat.name, ktype.get(key, ""), (variants.get(key, {}).get("Name") or "").strip())
        if item:
            items.append(item)
        else:
            log.warning("skipped pair (its category has no texture base)", extra={"extra_fields": {"pair": pid}})
    return _write(items, out_path, "keys")


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args[:1] == ["--keys"]:
        p = build_for_keys(args[1:])
    else:
        p = build()
    print(f"wrote {p}")
