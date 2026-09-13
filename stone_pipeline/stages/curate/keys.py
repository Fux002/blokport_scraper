"""Variant Keys and image names: the deterministic identity a new variety is created under.

{branch}_{type}_{name}_{uuid}, with a uuid5 in a fixed namespace, so the same new variety yields the same
Key every run (idempotent output), and the image base name IS the Key (a 1:1 image-to-variant link)."""

from __future__ import annotations

import re
import uuid

from stone_pipeline.config.settings import CATEGORIES, SETTINGS, active_categories
from stone_pipeline.core.text import ascii_fold

# deterministic namespace for variant key uuids (fixed, so keys are reproducible)
_KEY_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

# every category derives from the registry.
BRANCHES = tuple(c.name for c in CATEGORIES)


def active_branches() -> tuple[str, ...]:
    """Categories a new variety fans out into: active (Medusa category id set) AND
    fan_out. A category joins automatically once its pcat is set -- no code change.
    A new variety is created in each, but only its product-backed branch gets an
    image (see product_backed below)."""
    return tuple(c.name for c in active_categories() if c.fan_out)


def _slug_us(text: str) -> str:
    """Underscore slug matching the existing Key convention (agata_black). Accent-folded so the
    Key is fold-invariant -- same key whether the caller passes 'Porriño' or 'Porrino' -- matching
    title_case/slugify and so the deterministic Key can never split one variety in two."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", ascii_fold(text or "").casefold())).strip("_")


def gen_key(branch: str, stone_type: str, name: str) -> str:
    """{branch}_{type}_{name}_{uuid}, with a deterministic uuid5 so the same new
    variety yields the same key every run. Empty parts (e.g. a missing stone_type)
    are dropped so the key never has a double underscore (slab__name)."""
    u = uuid.uuid5(_KEY_NS, f"{branch}:{_slug_us(name)}")
    parts = [branch, _slug_us(stone_type), _slug_us(name)]
    return "_".join(p for p in parts if p) + f"_{u}"


def core(key: str, branch: str) -> str:
    """The branch-independent core of a Key (type + name slug): the identity the 'already exists in ANY
    category' check compares, so a variety one category carries is never re-minted in another."""
    return key[len(branch) + 1:].rsplit("_", 1)[0]


def image_filename(key: str) -> str:
    """The image base name IS the variant Key, so the image-to-variant link is a
    1:1, unambiguous identity (handles duplicate variety names automatically) and
    the same name is used in the variant Image and the backbone image_file. The
    image is no longer part of the tree join (that is by Key now), so the filename
    only needs to identify the variant unambiguously."""
    return f"{key}.png"


def image_url(filename: str) -> str:
    base = SETTINGS.curation.variant_image_base
    return f"{base}{filename}" if base else filename
