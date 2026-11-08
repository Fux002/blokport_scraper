"""Phase 1 variety Key rule (SC-1): mint the new `{type}__{name}_{uuid}` Key and the ONE parse of it.

OFFLINE and ADDITIVE. This is NOT the production type parser (`type_slug_from_key` and friends): that rewrite
is the Phase 1 code hold and the PROTECTED type area, untouched here. These helpers are used only by the union
key-map generator (`keymap.py`) and the paired vectors file, so SC-1 can run and prove itself with no change to
matching, curate, ledger or sync.

The slug is the repo's existing `_slug_us` (stages/curate/keys), so the name part of a new Key is byte-identical
to what curate already mints. The paired backend rule is `src/model/catalog/variety-key.ts` (154-fd P1-6); the
vectors file (`fixtures/variety_key_vectors.json`) pins both sides. No em dashes (design principle 2).
"""
from __future__ import annotations

import uuid

from stone_pipeline.stages.curate.keys import _KEY_NS, _slug_us

# The ONE separator between the type slug and the name slug (D1, section 2a). `_slug_us` collapses runs of
# `_`, so `__` never occurs inside a slug, and the type is the part before the FIRST `__`, one split.
SEP = "__"

# varietyKeyProblem values, paired with the backend enum (154-fd P1-6, unit 6.1).
NO_TYPE_SEPARATOR = "no_type_separator"
EMPTY_TYPE = "empty_type"
EMPTY_NAME = "empty_name"
SECOND_TYPE_SEPARATOR = "second_type_separator"


def new_key(type_name: str, name: str) -> str:
    """The new variety Key `{type slug}__{name slug}_{uuid5}` (D1).

    The uuid5 is in the repo's key namespace over the two SLUGS (`uuid5(_KEY_NS, "slug_type:slug_name")`), not
    the raw strings. That is the only basis a row-less Key can yield (a config.db retired/protected Key with no
    store row carries slugs and nothing else), and it makes every copy of one identity (its slab/block/tile
    Keys, and any retired-only Key) collapse onto the SAME new Key. `_slug_us` is idempotent on a slug, so
    passing a type name or an already-parsed slug gives the same result."""
    slug_type, slug_name = _slug_us(type_name), _slug_us(name)
    token = uuid.uuid5(_KEY_NS, f"{slug_type}:{slug_name}")
    return f"{slug_type}{SEP}{slug_name}_{token}"


def variety_key_problem(key: str) -> str | None:
    """The one check (paired with the backend's `varietyKeyProblem`). `None` for a Key of the form; otherwise
    the single problem. It reads no type list, so a prefixed Key (`slab_`/`block_`/`tile_`) is refused by the
    same rule, and the empty string is `no_type_separator`."""
    parts = key.split(SEP)
    if len(parts) == 1:
        return NO_TYPE_SEPARATOR
    if len(parts) > 2:
        return SECOND_TYPE_SEPARATOR
    type_part, name_part = parts
    if not type_part:
        return EMPTY_TYPE
    if not name_part:
        return EMPTY_NAME
    return None


def variety_key_type(key: str) -> str:
    """The type slug: the part before the first `__`, for a Key that passes `variety_key_problem`. Raises on
    any other, naming the Key and its problem, never a best guess (paired with `varietyKeyType`)."""
    problem = variety_key_problem(key)
    if problem is not None:
        raise ValueError(f"not a variety Key ({problem}): {key!r}")
    return key.split(SEP, 1)[0]
