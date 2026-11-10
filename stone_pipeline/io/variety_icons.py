"""The variety-icon S3 layout, the one place it is spelled (PHASE-1-SPEC section 4).

A variety has one icon per category it is shown in, at `{ENV_SEGMENT}/variations/{Key}/{category name}.png` in the
env's staging bucket. That full object key travels on the variation payload (`category_images[].s3_key`), so it is
untrusted input on Blokport's side, and the scraper only ever builds it here: from the env's segment, a Key that
passes the one parser (phase1.variety_key) and a pack category NAME. No em dashes (design principle 2).
"""

from __future__ import annotations

from stone_pipeline.config.categories import Category, category
from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.phase1.variety_key import variety_key_problem

VARIATIONS_PREFIX = f"{ENV_SEGMENT}/variations/"
_SUFFIX = ".png"
# emit's side file next to 1_variants_full.csv: {Key: {category name: ETag}}, each produced variety's icon pairs as
# listed on S3, which the ledger's populate reads. Written only when S3 was listed.
ICON_SIDECAR = "variant_image_shas.json"


def _pack_category(name: str) -> Category | None:
    """The pack category whose NAME is exactly `name` (category() also accepts a label, which a path never holds)."""
    cat = category(name)
    return cat if cat is not None and cat.name == name else None


def icon_key(key: str, category_name: str) -> str:
    """The full S3 object key of a variety's icon in one category. Refuses (ValueError) a Key the parser refuses and
    a name that is not a pack category's, so no other key can be built."""
    if (problem := variety_key_problem(key)) is not None:
        raise ValueError(f"variety icon: {key!r} is not a variety Key ({problem})")
    if _pack_category(category_name) is None:
        raise ValueError(f"variety icon of {key}: {category_name!r} is not a pack category name")
    return f"{VARIATIONS_PREFIX}{key}/{category_name}{_SUFFIX}"


def pair_id(key: str, category_name: str) -> str:
    """A pair's id, `{Key}/{category name}`: its icon's path under the prefix without the suffix (the best-model
    marker's entries). Built from icon_key, so it refuses what icon_key refuses."""
    return icon_key(key, category_name)[len(VARIATIONS_PREFIX):-len(_SUFFIX)]


def icon_pair(s3_key: str) -> tuple[str, Category] | None:
    """(Key, category) of a listed object that is a variety icon, or None for any other object under the prefix (a
    flat icon of the old layout, a marker file, a stray name), which the caller reports."""
    if not (s3_key.startswith(VARIATIONS_PREFIX) and s3_key.endswith(_SUFFIX)):
        return None
    key, sep, name = s3_key[len(VARIATIONS_PREFIX):-len(_SUFFIX)].partition("/")
    cat = _pack_category(name) if sep else None
    if cat is None or variety_key_problem(key) is not None:
        return None
    return key, cat
