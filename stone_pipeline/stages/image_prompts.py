"""Build the image-generation prompts file for variants that need an image.

Bridges the catalog's "to generate" list (the new backbone posts) to the fal.ai
generator (image_pipeline/genetate_images.py): one prompt per missing
variant, with output_name = the variant Key, so the generator writes {Key}.png
directly -- no rename step, and the file already matches the variant's Image URL
(dev/variations/{Key}.png).

The prompt format matches what the generator's build_prompt() expects:
  'Transform this attached image into <Type>: <Name>. <directives>'
so the existing preservation logic (keep slab geometry, white bg, drop the
composition-breakers) applies unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS, category_for_key
from stone_pipeline.core import logfmt

log = logfmt.get_logger("image_prompts")

# surface directives kept by the generator (composition-breakers are dropped there)
_DIRECTIVES = ("No shadows, no light glare, no reflections, no highlights, "
               "flat even lighting, no text, no watermark, straight-on flat view, "
               "no background, no perspective.")


def build(additions_dir: Path | None = None, out_path: Path | None = None) -> Path:
    additions_dir = Path(additions_dir or SETTINGS.paths.catalog_source_dir / "backbone_additions")
    out_path = Path(out_path or SETTINGS.paths.workspace_root
                    / "image_pipeline" / "prompts_to_generate.json")
    posts: list[dict] = []
    for f in sorted(additions_dir.glob("*.json")):   # the per-branch new-variety backbone deltas
        posts.extend(json.loads(f.read_text(encoding="utf-8")))

    items = []
    seen: set[str] = set()
    for p in posts:
        key = (p.get("key") or "").strip()
        if not key or key in seen:
            continue
        # only generate an image when a scraped product actually uses this variant
        # in this category; fan-out copies with no product are skipped (saves cost).
        # Default False: a post without the flag is NOT generated (no accidental
        # generation of a whole mirrored backbone, e.g. tiles).
        if not p.get("product_backed", False):
            continue
        # a category with no base image is NOT texture-generated (it uses real
        # product photos via the inbox, e.g. accessories; tiles until a base is set)
        cat = category_for_key(key)
        if not cat or not cat.base_image:
            continue
        seen.add(key)
        base = cat.base_image
        items.append({
            "output_name": key,  # generator writes {key}.png
            "base_image_url": base,
            "prompt": f"Transform this attached image into {p.get('stone_type','')}: "
                      f"{p.get('variant','')}. {_DIRECTIVES}",
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("image prompts built", extra={"extra_fields": {"prompts": len(items), "out": str(out_path)}})
    return out_path


if __name__ == "__main__":
    p = build()
    print(f"wrote {p}")
