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

import dataclasses
import functools
from stone_pipeline.core import env
from dataclasses import dataclass
from pathlib import Path

import yaml

_DOMAINS_DIR = Path(__file__).parent / "domains"
_DEFAULT_PACK = "stone"
_REQUIRED = ("name", "attributes", "disambiguator", "leaf_attributes", "categories",
             "ambiguous_type_words", "generic_descriptors", "generic_material_word",
             "default_finishes", "fallback_color",
             "last_resort_finishes", "last_resort_quality", "block_finish",
             "dimension_ranges", "dimension_defaults", "finish_phrases", "finish_phrase_default",
             "default_density")


@dataclass(frozen=True)
class FreeSizeShapes:
    """The shapes a category's free-size listing is offered in. Each ratio is one shape's long:short side (1 a
    square). A side the vendor states is the short side and the free side is ratio x it; with every side free
    the long side is `long_side`. A shape longer than `max_length` is not offered. Metres."""
    ratios: tuple[float, ...]
    long_side: float
    max_length: float


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
    # words that, as the ONLY difference between two variety names, signal an alias not a distinct variety
    # (base + descriptor: 'Bardiglio Nuvolato' ~ '... Marble'). Per-domain: each pack supplies its own.
    generic_descriptors: frozenset[str]
    # the generic material noun of this domain ('stone'; 'wood' for a wood pack): used as the title's
    # material fallback and stripped when deciding whether a type name is purely generic.
    generic_material_word: str
    default_finishes: tuple[str, ...]
    fallback_color: str
    last_resort_finishes: dict[str, str]   # branch -> flagged last-resort finish (unlisted branch uses slab's)
    last_resort_quality: str               # flagged last-resort quality when a source states none
    block_finish: str                      # the standard finish applied to a raw/unfinished block
    # format -> {weight|length|width|height: (lo, hi)} in metres/tonnes
    dimension_ranges: dict[str, dict[str, tuple[float, float]]]
    # format -> {length, height, thickness} fallback size in metres (derive fills a missing dim from this)
    dimension_defaults: dict[str, dict[str, float]]
    finish_phrases: dict[str, str]
    finish_phrase_default: str
    # material density in kg/m3, used to derive weight when a source omits it. This is the fallback when
    # reference/type_density.csv is absent or lacks the row's type (that CSV's 'default' row overrides it when
    # present). Required per pack so a non-stone material never silently inherits stone's ~2700 (stone: 2700
    # marble; wood ~700; lime ~2500). Wrong density = wrong derived weight + freight, silently.
    default_density: float
    # Name-cleaning corpus rules (core/text.py), optional. name_code_pattern: a regex for codey-LOOKING tokens
    # that are real names in this domain and must never be stripped/flagged (stone: granite 'G682').
    # trailing_grade_letters: whether a trailing lone letter is a grade code to strip in cleaning and flag in
    # review (stone: 'Rosal C' -> 'Rosal'). A pack that omits both never mangles a name on these rules.
    name_code_pattern: str | None = None
    trailing_grade_letters: bool = False
    # Classify a variety's colour from its product image with the stone-tuned CV palette (variety_color).
    # A domain whose tones that palette cannot read sets this false: colour then comes from the pack
    # fallback_color / the Medusa-supplied value, and its CLASSIFIABLE_COLORS need not exist in Medusa.
    classify_texture_color: bool = True
    # Product-FORM words -- a physical form or trade unit (step, sill, coping, sample, cross-cut ...) that
    # never carries variety identity. The name cleaner strips them unconditionally, like the category
    # format words, and the alias judge treats the single-token ones as generic. Optional: a pack that
    # declares none strips nothing extra. Entries are lowercase words or space-separated phrases; a hyphen
    # in a scraped name matches a space here ('Cross-cut' == 'cross cut'). Every entry must be
    # collision-checked against the domain's real variety names before it is added (see stone.yaml).
    product_form_words: frozenset[str] = frozenset()
    # Matching / availability vocabulary that used to be stone-shaped constants in code. Every pack
    # declares its own (empty is a valid declaration): supplier inventory prefixes stripped before an exact
    # match ('Z', 'ZB'); trailing render/finish tags stripped there too (a thickness '3cm' and 'grade X' are
    # language-level and always stripped); spelling variants of ONE colour word (gray -> grey) so a spelling
    # never reads as a colour conflict; wrapper words dropped when composing a compound attribute value;
    # the attribute whose words count as colours for match blocking; the supplier words that mean in stock
    # without a count, made to order (on sale with no count) and out of stock (a trusted sold-out); and the
    # categories whose listing is one physical piece, so 'in stock' there is a count of 1.
    inventory_prefixes: tuple[str, ...] = ()
    trailing_render_tags: tuple[str, ...] = ()
    spelling_variants: dict[str, str] = dataclasses.field(default_factory=dict)
    join_noise_words: tuple[str, ...] = ()
    color_attribute: str = "color"
    in_stock_words: tuple[str, ...] = ()
    made_to_order_words: tuple[str, ...] = ()
    out_of_stock_words: tuple[str, ...] = ()
    single_piece_categories: frozenset[str] = frozenset()
    # Free size: a supplier word for a side cut to order ('Free' length), and per category the shapes such a
    # listing is offered in, one listing per shape (stages/free_size). A category with no entry offers none:
    # its free side stays a defaulted size, as before.
    free_size_words: tuple[str, ...] = ()
    free_size_shapes: dict[str, FreeSizeShapes] = dataclasses.field(default_factory=dict)


def _pack_path(name: str) -> Path:
    return _DOMAINS_DIR / f"{name}.yaml"


# The attributes the ROW SCHEMA carries (CanonicalRow has <attribute>_name / <attribute>_id for exactly
# these) and the identity attribute the Key builder, reconcile and curate implement by name. A pack may use a
# subset, in any order, but cannot declare an attribute the row has no field for, nor another identity
# attribute: that is a row-schema change, not a pack change, and the loader says so instead of accepting a
# declaration the pipeline would silently ignore. A test ties this list to the row model.
ROW_ATTRIBUTES = ("type", "color", "finish", "quality")
IDENTITY_ATTRIBUTE = "type"

# the keys each category dict must carry (settings.py reads these to build a Category); the rest are optional.
_VOCABULARY_LISTS = ("inventory_prefixes", "trailing_render_tags", "join_noise_words", "in_stock_words",
                     "made_to_order_words", "out_of_stock_words", "free_size_words")
# The three stock states a supplier word can state; one word means one of them.
_STOCK_WORD_LISTS = ("in_stock_words", "made_to_order_words", "out_of_stock_words")

_CATEGORY_KEYS = ("name", "plural", "label", "backbone_filename", "base_image", "shares_variety_vocab", "fan_out")


def _validate_shape(name: str, path: Path, data: dict) -> None:
    """Fail LOUD at load on a malformed NESTED shape (a category dict missing a key, or a dimension range
    that is not a 2-number pair) OR an internally INCONSISTENT pack (a category with no entry in a
    per-category map, a disambiguator outside the attribute set), naming the pack + the problem -- instead
    of a raw KeyError/ValueError surfacing deep in a stage (settings._build_categories /
    derive.derive_dimensions / the Key builder) much later on a new (e.g. wood) pack."""
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
    for fmt, dims in data["dimension_defaults"].items():
        for dim in ("length", "width", "height"):
            if not isinstance(dims.get(dim), (int, float)) or dims[dim] <= 0:
                bad(f"dimension_defaults[{fmt!r}][{dim!r}] must be a positive number, got {dims.get(dim)!r}")
    if not isinstance(data["default_density"], (int, float)) or isinstance(data["default_density"], bool) \
            or data["default_density"] <= 0:
        bad(f"default_density must be a positive number (kg/m3), got {data['default_density']!r}")

    # Cross-checks: the per-field shapes above are each individually valid, but the pack can still be
    # internally INCONSISTENT in a way that only KeyErrors deep in a stage on a new pack. Catch it at load.
    cat_names = {cat["name"] for cat in data["categories"]}   # every category has 'name' (validated above)
    # V1: every category needs an entry in each per-category map, and no map may carry a non-category key.
    # last_resort_finishes is deliberately EXCLUDED -- a branch may be absent by design and fall back to slab's.
    for map_name in ("dimension_ranges", "dimension_defaults"):
        keys = set(data[map_name])
        if cat_names - keys:
            bad(f"{map_name} is missing an entry for categories {sorted(cat_names - keys)}")
        if keys - cat_names:
            bad(f"{map_name} has key(s) {sorted(keys - cat_names)} not in the declared categories {sorted(cat_names)}")
    # a typo here would silently hold every in-stock listing of the real category
    if not isinstance(data.get("single_piece_categories") or [], list):
        bad("'single_piece_categories' must be a list of category names")
    if unknown_pieces := set(data.get("single_piece_categories") or []) - cat_names:
        bad(f"single_piece_categories {sorted(unknown_pieces)} are not declared categories {sorted(cat_names)}")
    # V2: the disambiguator (the identity attribute that drives the Key) must be one of the attributes.
    if data["disambiguator"] not in data["attributes"]:
        bad(f"disambiguator {data['disambiguator']!r} is not in attributes {list(data['attributes'])}")
    # V6: the fields the pipeline formats with must be well-formed here, not deep in a stage (checked when
    # present: the required-key check runs first in load_pack; a shape test may pass a partial pack).
    if "finish_phrases" in data and not isinstance(data["finish_phrases"], dict):
        bad("'finish_phrases' must be a mapping of finish -> phrase")
    if "default_finishes" in data and not data["default_finishes"]:
        bad("'default_finishes' is empty: a minted variety needs at least one finish to be priceable")
    if "generic_material_word" in data and not str(data["generic_material_word"]).strip():
        bad("'generic_material_word' is empty: the title and description fall back to it")
    for words in _VOCABULARY_LISTS:
        if words in data and not isinstance(data[words], list):
            bad(f"'{words}' must be a list")
    seen: dict[str, str] = {}
    for words in _STOCK_WORD_LISTS:
        for word in data.get(words) or []:
            key = fold_word(word)
            if key in seen and seen[key] != words:
                bad(f"stock word {key!r} is in both {seen[key]} and {words}: one word states one stock state")
            seen[key] = words
    shapes = data.get("free_size_shapes") or {}
    if not isinstance(shapes, dict):
        bad("'free_size_shapes' must be a mapping of category -> {ratios, long_side, max_length}")

    def number(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    for fmt, spec in shapes.items():
        where = f"free_size_shapes[{fmt!r}]"
        if fmt not in cat_names:
            bad(f"{where} is not in the declared categories {sorted(cat_names)}")
        if not isinstance(spec, dict) or set(spec) != {"ratios", "long_side", "max_length"}:
            bad(f"{where} must be a mapping of exactly ratios, long_side, max_length")
        ratios = spec["ratios"]
        if not isinstance(ratios, list) or not ratios or not all(number(r) and r >= 1 for r in ratios):
            bad(f"{where}.ratios must be a non-empty list of long:short ratios >= 1, got {ratios!r}")
        if len(set(ratios)) != len(ratios):
            bad(f"{where}.ratios must be distinct (one listing each), got {ratios!r}")
        for side in ("long_side", "max_length"):
            if not number(spec[side]) or spec[side] <= 0:
                bad(f"{where}.{side} must be a positive number (metres), got {spec[side]!r}")
    if "spelling_variants" in data and not isinstance(data["spelling_variants"], dict):
        bad("'spelling_variants' must be a mapping of variant -> canonical word")
    if "color_attribute" in data and data["color_attribute"] not in data["attributes"]:
        bad(f"'color_attribute' must name one of the attributes {list(data['attributes'])}")
    # V3: category roles. Exactly one category is the default_form (the unresolved-format + finish/dim
    # fallback, which the pipeline always needs); at most one is the bulk_form (the uncut/solid form).
    default_forms = [c.get("name") for c in data["categories"] if c.get("default_form")]
    bulk_forms = [c.get("name") for c in data["categories"] if c.get("bulk_form")]
    if len(default_forms) != 1:
        bad(f"exactly one category must set default_form: true, found {default_forms or 'none'}")
    if len(bulk_forms) > 1:
        bad(f"at most one category may set bulk_form: true, found {bulk_forms}")
    # V4: leaf_attributes must be a subset of attributes, and MUST exclude the disambiguator (the docstring
    # invariant leaf_attributes == attributes - {disambiguator}). Otherwise a stage that fills/reconciles a
    # leaf attribute or the Key builder KeyErrors deep on a new pack instead of failing loud here.
    leaf = set(data["leaf_attributes"])
    attrs = set(data["attributes"])
    if leaf - attrs:
        bad(f"leaf_attributes {sorted(leaf - attrs)} are not in attributes {sorted(attrs)}")
    if data["disambiguator"] in leaf:
        bad(f"disambiguator {data['disambiguator']!r} must NOT be in leaf_attributes {sorted(leaf)}")
    # V5: a category's mirror_of must name an existing category (a typo'd mirror silently mirrors nothing).
    for cat in data["categories"]:
        mo = cat.get("mirror_of")
        if mo is not None and mo not in cat_names:
            bad(f"category {cat['name']!r} mirror_of {mo!r} is not a declared category {sorted(cat_names)}")
    # V7 (F-5): the attributes must be ones the ROW SCHEMA carries, and the identity attribute must be the
    # one the pipeline implements by name. A declaration outside that is a row-schema change, refused here
    # instead of being silently ignored downstream.
    unknown = [a for a in data["attributes"] if a not in ROW_ATTRIBUTES]
    if unknown:
        bad(f"attributes {unknown} are not in the row schema {list(ROW_ATTRIBUTES)}: CanonicalRow carries "
            f"<attribute>_name/_id fields for exactly those, so a new attribute is a row-schema change")
    if data["disambiguator"] != IDENTITY_ATTRIBUTE:
        bad(f"disambiguator must be {IDENTITY_ATTRIBUTE!r} (the identity attribute the Key builder, reconcile "
            f"and curate implement by name), got {data['disambiguator']!r}")


def load_pack(name: str | None = None) -> DomainPack:
    """Load a pack by name (env DOMAIN_PACK, default 'stone'). Raises on a missing pack or a missing required
    key -- never silently substitutes a default vocabulary. The env var is brand-neutral (DOMAIN_PACK): the
    product type is not a brand's property. The legacy BLOKPORT_DOMAIN_PACK is still read for back-compat."""
    name = name or env.getenv("DOMAIN_PACK") or _DEFAULT_PACK
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
        generic_descriptors=frozenset(data["generic_descriptors"]),
        generic_material_word=data["generic_material_word"],
        default_finishes=tuple(data["default_finishes"]),
        fallback_color=data["fallback_color"],
        last_resort_finishes=dict(data["last_resort_finishes"]),
        last_resort_quality=data["last_resort_quality"],
        block_finish=data["block_finish"],
        dimension_ranges=ranges,
        dimension_defaults={fmt: {k: float(v) for k, v in dims.items()}
                            for fmt, dims in data["dimension_defaults"].items()},
        finish_phrases=dict(data["finish_phrases"]),
        finish_phrase_default=data["finish_phrase_default"],
        default_density=float(data["default_density"]),
        name_code_pattern=data.get("name_code_pattern"),
        trailing_grade_letters=bool(data.get("trailing_grade_letters", False)),
        classify_texture_color=bool(data.get("classify_texture_color", True)),
        product_form_words=frozenset(str(w).strip().lower() for w in (data.get("product_form_words") or [])
                                     if str(w).strip()),
        # optional vocabulary (a pack that declares none strips/recognises nothing extra)
        inventory_prefixes=_words(data.get("inventory_prefixes") or []),
        trailing_render_tags=_words(data.get("trailing_render_tags") or []),
        spelling_variants={str(k).strip().lower(): str(v).strip().lower()
                           for k, v in (data.get("spelling_variants") or {}).items()},
        join_noise_words=_words(data.get("join_noise_words") or []),
        color_attribute=data.get("color_attribute", "color"),
        in_stock_words=_words(data.get("in_stock_words") or []),
        made_to_order_words=_words(data.get("made_to_order_words") or []),
        out_of_stock_words=_words(data.get("out_of_stock_words") or []),
        single_piece_categories=frozenset(data.get("single_piece_categories") or []),
        free_size_words=_words(data.get("free_size_words") or []),
        free_size_shapes={fmt: FreeSizeShapes(tuple(float(r) for r in spec["ratios"]), float(spec["long_side"]),
                                              float(spec["max_length"]))
                          for fmt, spec in (data.get("free_size_shapes") or {}).items()},
    )


def fold_word(text) -> str:
    """The one form a pack word and the supplier text it is matched against are compared in: lower case, single
    spaces, no edge whitespace. The pack loader stores words in it and derive folds the supplier text the same way."""
    return " ".join(str(text or "").lower().split())


def _words(raw) -> tuple[str, ...]:
    return tuple(w for w in map(fold_word, raw) if w)


@functools.lru_cache(maxsize=1)
def active_pack() -> DomainPack:
    """The active domain pack, cached. Fails loud on a missing/invalid pack. Tests that switch
    BLOKPORT_DOMAIN_PACK must call active_pack.cache_clear()."""
    return load_pack()
