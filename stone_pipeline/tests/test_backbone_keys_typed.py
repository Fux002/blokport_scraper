"""Every keyed post in the committed backbones states its Key's type: the Key `{category}_{type}_{name}_{uuid}` begins
with the post's own category and the slug of the post's stated type. A post that carries the Key of another type makes
one Key mean two types (the Phase 1 SC-1 key map aborts on that as a type_conflict). On 8 Nov six block Keys sat on the
same-named post of the type that sorts first (Ocean Blue: the Marble block Key on the Granite post), owner ruling
"move the Keys". A post that states no type (stone_type '') has no type to compare: SC-1 holds it as untyped, so it is
listed here, not checked."""

from __future__ import annotations

import json
from pathlib import Path

from stone_pipeline.config.categories import CATEGORIES
from stone_pipeline.config.settings import SETTINGS
# the rule gen_key writes a Key's type segment with (stages/curate/keys._slug_us is slugify with "_")
from stone_pipeline.core.text import slugify


def _backbones() -> list[tuple[str, Path]]:
    """(category, file) of every committed backbone: the main file of each category and its backbone_additions."""
    additions = sorted(Path(SETTINGS.paths.backbone_additions_dir).glob("*.json"))
    return [(c.name, Path(c.backbone_path)) for c in CATEGORIES] + [(p.stem, p) for p in additions]


def _posts(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return document["posts"] if isinstance(document, dict) else document


def test_every_keyed_post_states_its_key_s_type():
    checked: dict[str, int] = {}
    untyped = []
    wrong = []
    for category, path in _backbones():
        for post in _posts(path):
            key = (post.get("key") or "").strip()
            if not key:
                continue
            stated = (post.get("stone_type") or "").strip()
            if not stated:
                untyped.append(f"{path.name}: {key}")
                continue
            checked[category] = checked.get(category, 0) + 1
            if not key.startswith(f"{category}_{slugify(stated, '_')}_"):
                wrong.append(f"{path.name} variant_id {post.get('variant_id')} {post.get('variant')!r} states "
                             f"{stated} but carries {key}")
    assert {"slab", "block", "tile"} <= set(checked)            # every category's backbone was read
    print(f"{len(untyped)} keyed posts state no type (SC-1 holds them as untyped):", untyped)
    assert wrong == []
