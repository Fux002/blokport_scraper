"""Build the Tiles backbone tree as a mirror of the Slabs backbone.

Tiles share the slab finish set (blocks differ), so the tile allowed-combinations
tree is the slab backbone with category 'Tiles' and a tile_ Key per variety. The
finishes/colours/qualities are copied from each slab post verbatim, so tiles allow
exactly what slabs allow. Keys are deterministic (gen_key), so re-running is stable
and dev/prod match.

Run:  python -m stone_pipeline.stages.build_tile_backbone
"""

from __future__ import annotations

import json

from stone_pipeline.config.settings import CATEGORIES, category
from stone_pipeline.core import logfmt
from stone_pipeline.stages.curate import gen_key, image_filename

log = logfmt.get_logger("build_tile_backbone")


def build_one(cat) -> int:
    """Build the backbone of a category that mirrors another (cat.mirror_of): same
    varieties/finishes/colours/qualities, with this category's label + deterministic
    Key. Tiles mirror slabs; any future mirroring category works the same way."""
    source = category(cat.mirror_of)
    src_posts = json.loads(source.backbone_path.read_text(encoding="utf-8-sig"))["posts"]
    posts = []
    for p in src_posts:
        key = gen_key(cat.name, p["stone_type"], p["variant"])  # deterministic Key
        posts.append({
            "key": key,
            "variant": p["variant"],
            "category": cat.label,
            "stone_type": p["stone_type"],
            "color": p.get("color", []),
            "finishes": p.get("finishes", []),     # SAME finishes as the mirror source
            "qualities": p.get("qualities", []),
            "aliases": p.get("aliases", []),
            "image_file": image_filename(key),
            "exists": "no",
            "in_csv": "no",
            "merged_from": [],
        })
    cat.backbone_path.write_text(json.dumps({"posts": posts}, indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    log.info("mirror backbone built", extra={"extra_fields": {
        "category": cat.name, "mirror_of": cat.mirror_of, "posts": len(posts)}})
    return len(posts)


def build() -> dict:
    """Build every mirror-backed category's backbone (registry-driven)."""
    return {c.name: build_one(c) for c in CATEGORIES if c.mirror_of}


if __name__ == "__main__":
    for name, n in build().items():
        print(f"wrote {category(name).backbone_path} ({n} {name} posts, mirror of {category(name).mirror_of})")
