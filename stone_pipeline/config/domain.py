"""Product-domain pack loader (Phase 3: abstraction from the natural-stone suite).

The pipeline logic is product-agnostic; the DOMAIN vocabulary (attribute set, guard words, defaults, ranges,
finish phrases, and -- in later steps -- the category model) is declared in a pack YAML, not hardcoded in the
stages. `stone` is the default pack and reproduces the historical constants EXACTLY, so existing output is
byte-identical. A different product type is a sibling pack (config/domains/<type>.yaml) selected by
BLOKPORT_DOMAIN_PACK -- no code edit.

Fail loud: a missing or malformed pack raises at load, never a silent fallback that would ship the wrong
vocabulary. The active pack is cached (cleared in tests that switch packs).
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_DOMAINS_DIR = Path(__file__).parent / "domains"
_DEFAULT_PACK = "stone"
_REQUIRED = ("name", "attributes", "disambiguator", "leaf_attributes", "categories",
             "ambiguous_type_words", "default_finishes", "fallback_color",
             "last_resort_finishes", "last_resort_quality", "block_finish",
             "dimension_ranges", "finish_phrases", "finish_phrase_default")


@dataclass(frozen=True)
class DomainPack:
    """The declared vocabulary + defaults for one product type. Grows as more hardcoded constants move here
    (the category model lands in a later Phase 3 step)."""
    name: str
    # the attribute vocabulary, in resolution order (was normalize.VOCAB_FIELDS / loaders.VOCAB_CATEGORIES)
    attributes: tuple[str, ...]
    disambiguator: str                 # the identity-defining attribute (drives the Key; never guessed)
    leaf_attributes: tuple[str, ...]   # attributes that grow the backbone leaf (all but the disambiguator)
    # the category model: one dict per category (name/plural/label/backbone_filename/base_image/flags/...).
    # settings.py maps these to Category objects (the pack stays agnostic to that dataclass); pcat is env.
    categories: tuple[dict, ...]
    ambiguous_type_words: frozenset[str]
    default_finishes: tuple[str, ...]
    fallback_color: str
    last_resort_finishes: dict[str, str]   # branch -> flagged last-resort finish (unlisted branch uses slab's)
    last_resort_quality: str               # flagged last-resort quality when a source states none
    block_finish: str                      # the standard finish applied to a raw/unfinished block
    # format -> {weight|length|width|height: (lo, hi)} in metres/tonnes
    dimension_ranges: dict[str, dict[str, tuple[float, float]]]
    finish_phrases: dict[str, str]
    finish_phrase_default: str


def _pack_path(name: str) -> Path:
    return _DOMAINS_DIR / f"{name}.yaml"


# the keys each category dict must carry (settings.py reads these to build a Category); the rest are optional.
_CATEGORY_KEYS = ("name", "plural", "label", "backbone_filename", "base_image", "shares_variety_vocab", "fan_out")


def _validate_shape(name: str, path: Path, data: dict) -> None:
    """Fail LOUD at load on a malformed NESTED shape (a category dict missing a key, or a dimension range
    that is not a 2-number pair), naming the pack + the problem -- instead of a raw KeyError/ValueError
    surfacing deep in a stage (settings._build_categories / derive.derive_dimensions) much later."""
    def bad(msg: str):
        raise ValueError(f"domain pack {name!r} at {path} is malformed: {msg}")

    if not data["attributes"]:
        bad("'attributes' is empty")
    if not isinstance(data["last_resort_finishes"], dict):
        bad("'last_resort_finishes' must be a mapping of branch -> finish")
    for i, cat in enumerate(data["categories"]):
        if not isinstance(cat, dict):
            bad(f"categories[{i}] is not a mapping")
        absent = [k for k in _CATEGORY_KEYS if k not in cat]
        if absent:
            bad(f"categories[{i}] ({cat.get('name', '?')}) is missing keys: {absent}")
    for fmt, dims in data["dimension_ranges"].items():
        for dim, vals in dims.items():
            if not (isinstance(vals, (list, tuple)) and len(vals) == 2
                    and all(isinstance(v, (int, float)) for v in vals)):
                bad(f"dimension_ranges[{fmt!r}][{dim!r}] must be a [lo, hi] pair of numbers, got {vals!r}")


def load_pack(name: str | None = None) -> DomainPack:
    """Load a pack by name (BLOKPORT_DOMAIN_PACK, default 'stone'). Raises on a missing pack or a missing
    required key -- never silently substitutes a default vocabulary."""
    name = name or os.environ.get("BLOKPORT_DOMAIN_PACK", _DEFAULT_PACK)
    path = _pack_path(name)
    if not path.exists():
        raise FileNotFoundError(
            f"domain pack {name!r} not found at {path}. Available: "
            f"{sorted(p.stem for p in _DOMAINS_DIR.glob('*.yaml'))}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise ValueError(f"domain pack {name!r} at {path} is missing required keys: {missing}")
    _validate_shape(name, path, data)
    ranges = {fmt: {dim: tuple(vals) for dim, vals in dims.items()}
              for fmt, dims in data["dimension_ranges"].items()}
    return DomainPack(
        name=data["name"],
        attributes=tuple(data["attributes"]),
        disambiguator=data["disambiguator"],
        leaf_attributes=tuple(data["leaf_attributes"]),
        categories=tuple(data["categories"]),
        ambiguous_type_words=frozenset(data["ambiguous_type_words"]),
        default_finishes=tuple(data["default_finishes"]),
        fallback_color=data["fallback_color"],
        last_resort_finishes=dict(data["last_resort_finishes"]),
        last_resort_quality=data["last_resort_quality"],
        block_finish=data["block_finish"],
        dimension_ranges=ranges,
        finish_phrases=dict(data["finish_phrases"]),
        finish_phrase_default=data["finish_phrase_default"],
    )


@functools.lru_cache(maxsize=1)
def active_pack() -> DomainPack:
    """The active domain pack, cached. Fails loud on a missing/invalid pack. Tests that switch
    BLOKPORT_DOMAIN_PACK must call active_pack.cache_clear()."""
    return load_pack()
