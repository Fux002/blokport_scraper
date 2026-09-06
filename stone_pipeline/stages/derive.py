"""Stage 6: field derivation (section 7 Stage 6, worked ladders in section 10).

Runs the remaining resolvers in dependency order: category and units first, then
bundle and dimensions and origin, then title (needs variety/finish/format), then
description (needs origin/finish), then handle (needs title). Each resolver is a
trust-ordered strategy ladder, so the ordering is a dependency declaration, not
bespoke control flow. Never guesses a value above its floor into output; a
low-confidence fill still emits but always carries its flag.
"""

from __future__ import annotations

import csv
import functools
import json
import re

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import CATEGORIES, SETTINGS, Confidence, bulk_form_name, category, default_form_name
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.manifest import StageMetric
from stone_pipeline.core.numbers import normalize_unit, parse_number
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.stages._rowguard import isolate_rows
from stone_pipeline.gates.report import DEGRADED, OK
from stone_pipeline.core.text import match_key, slugify, title_case
from stone_pipeline.reference.loaders import ReferenceData

log = logfmt.get_logger("derive")

_NUM_UNIT = re.compile(r"(-?\d[\d.,]*\d|-?\d)\s*([a-zµ\"'″′’”]+)?", flags=re.IGNORECASE)
# a range 'lo - hi [unit]' ('2-3 cm', '2 - 3 cm', '20–30 mm'): the unit trails the WHOLE range, so
# the old first-number-only parse read '2-3 cm' as 2 (then metres). Average the endpoints and take
# the trailing unit. Requires a digit before the dash, so a lone negative '-3' is NOT a range.
_RANGE = re.compile(r"(\d[\d.,]*)\s*[-–—]\s*(\d[\d.,]*)\s*([a-zµ\"'″′’”]+)?", flags=re.IGNORECASE)
# a DIMENSION range 'lo to hi [unit]' / 'lo-hi [unit]' ('105 to 145cm', '2-3 cm'): the unit trails the whole
# range. Face dimensions take the MAX endpoint ("cut smaller later"); thickness never uses a range. Kept
# separate from _RANGE (which midpoints, for weight/generic callers) so their behaviour is unchanged.
_DIM_RANGE = re.compile(r"(\d[\d.,]*)\s*(?:to|[-–—])\s*(\d[\d.,]*)\s*([a-zµ\"'″′’”]+)?", flags=re.IGNORECASE)
# per-slab key in a scraped slabs-array, case/space/quote-insensitive ('"n":', "'N' :", '"Numero":')
_SLAB_KEY = re.compile(r"""["']\s*(?:n|numero)\s*["']\s*:""", flags=re.IGNORECASE)


def _count_slab_entries(raw: str) -> int:
    """Number of slabs in a scraped slabs-array. Prefer a real JSON parse (count list items); fall
    back to a case/space/quote-insensitive key count, so a spacing or casing variant of the per-slab
    key ('"N" :', "'n'", '"Numero"') is not silently read as 0 (the old literal .count('\"n\"') bug)."""
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return len(data)
    except (ValueError, TypeError):
        pass
    return len(_SLAB_KEY.findall(raw or ""))

# section 10.2 branch dimension/weight ranges (fallback only; parsed dims win) and the section 10.4
# finish-to-surface-phrase table now live in the active product-domain pack (config/domains/<pack>.yaml),
# so a different product type declares its own ranges/phrases. The stone pack reproduces the historical
# values exactly, so output is unchanged.


def _conf_name(c: Confidence) -> str:
    return Confidence(c).name


def _parse_measure(text: str, ref: ReferenceData, expect_dimension: str | None = None) -> float | None:
    """Parse a single '2cm' / '2.80m' measure to metres via units.csv. When `expect_dimension` is given
    (the weight path passes "weight"), a PRESENT token of a different dimension is rejected (returns None)
    instead of scaling as if it were the expected unit -- so a length/area token that leaked into the
    weight field falls through to the volume x density fallback rather than shipping a wrong kg. A bare
    (unit-less) number is unaffected: it stays the caller's default unit, as before."""
    if not text:
        return None
    # a range ('2-3 cm') resolves to the midpoint with the trailing unit, NOT the low endpoint as
    # unit-less metres. Checked first so '2-3 cm' can't fall through to the single-number branch.
    rng = _RANGE.search(text)
    if rng:
        lo, hi = parse_number(rng.group(1)), parse_number(rng.group(2))
        if lo is not None and hi is not None:
            raw_token = (rng.group(3) or "").strip()
            converted = ref.units.convert((lo + hi) / 2.0, normalize_unit(rng.group(3)), expect=expect_dimension)
            if converted is not None:
                return converted
            return (lo + hi) / 2.0 if not raw_token else None
    match = _NUM_UNIT.search(text)
    if not match:
        return None
    value = parse_number(match.group(1))
    if value is None:
        return None
    raw_token = (match.group(2) or "").strip()
    token = normalize_unit(match.group(2))   # quote marks -> in/ft; empty -> m
    converted = ref.units.convert(value, token, expect=expect_dimension)
    if converted is not None:
        return converted
    # convert failed: trust the bare number ONLY when no unit was given (already metres). An UNKNOWN
    # unit token ('x' from a '120x60' dimension, a typo'd abbreviation) is NOT metres -> unparseable,
    # so the caller synthesizes/flags instead of shipping a wildly wrong magnitude as a confident value.
    return value if not raw_token else None


# --- category (section 7 Stage 6) ---------------------------------------------
def derive_category(row: CanonicalRow, ref: ReferenceData) -> None:
    """Map the already-resolved format (format_resolve stage) to a backend
    category. Each active category routes to its OWN pcat via
    category_pcat_for_branch (Blocks->Blocks, Slabs->Slabs, Tiles->Tiles once the
    tile category id is set). If the format stage has not run, resolve it now as a
    safety net so this stage never depends on ordering."""
    from stone_pipeline.stages.format_resolve import branch_of, category_pcat_for_branch, resolve_format

    if not row.format_value:
        resolve_format(row, ref)
    row.category_pcat_id = category_pcat_for_branch(branch_of(row), ref)
    row.category_method = f"format:{row.format_method or 'unknown'}"


# --- dimensions and weight (section 10.2) -------------------------------------
def _derive_weight(row: CanonicalRow, ref: ReferenceData,
                   length: float | None, width: float | None, height: float | None) -> tuple[float | None, str]:
    """Weight in TONNES: the scraped value (kg/1000) when present, else volume x per-type density from
    the real dims (type_density.csv). None when a dimension is missing (validate then rejects on the
    dims). A derived weight is flagged. Returns (weight, method)."""
    # expect_dimension="weight": a PRESENT non-mass token in the weight field (a leaked length/area, e.g.
    # "2.5 m") is rejected -> falls through to volume x density below, instead of scaling as kg. A bare
    # number stays kg (suppliers ship bare kg; the zucchi adapter emits one by construction).
    kg = _parse_measure(row.raw_weight, ref, expect_dimension="weight") if row.raw_weight else None
    if kg is not None and kg > 0:
        return kg / 1000.0, "weight:parsed"
    if not all(v is not None and v > 0 for v in (length, width, height)):
        return None, "weight:missing"
    density = _type_density(row.type_name)
    weight = round((length * width * height * density) / 1000.0, 3)
    row.add_flag(ReviewFlag(field="weight", code=FlagCode.weight_derived,
                            best_guess=f"{weight}t ({row.type_name or 'default'} density {density:g})",
                            confidence=Confidence.medium, method="volume_x_density", src_url=row.src_url))
    return weight, "weight:derived"


_FACE_DIMS = ("length", "height")


def _dimension_category(row: CanonicalRow) -> str:
    """The row's dimension bucket (a category name): the bulk form when is_block (stone: block), else the
    row's own format if it is a known category (a tile keeps its own size), else the default form (slab)."""
    fmt = (row.format_value or "").casefold()
    bulk = bulk_form_name()
    if row.is_block or (bulk and fmt == bulk):
        return bulk
    return fmt if category(fmt) else default_form_name()


def _parse_face(text: str, ref: ReferenceData) -> float | None:
    """A face dimension (length/height) in metres: a range resolves to its MAX endpoint, a single measure
    parses normally. None when absent/unparseable, so the caller fills the pack default."""
    if not text:
        return None
    rng = _DIM_RANGE.search(text)
    if rng:
        return _parse_measure(f"{rng.group(2)}{rng.group(3) or ''}", ref)   # hi endpoint carries the unit
    return _parse_measure(text, ref)


def _parse_thickness(text: str, ref: ReferenceData) -> float | None:
    """The depth (canonical `width`) in metres: only a single clean measure counts. A range ('2-3cm') or a
    non-numeric token ('MULTI') is AMBIGUOUS -> None, so the caller fills the standard thickness (a 2-3cm
    slab is stocked as the 2cm standard, not the midpoint)."""
    if not text or _DIM_RANGE.search(text):
        return None
    return _parse_measure(text, ref)


def _dim_fetch_failed(row: CanonicalRow, field: str) -> bool:
    """True when THIS dimension's source fetch FAILED (recoverable), so it must be HELD, not defaulted. A
    scraper marks the field-group in fetch_failed_fields: a blanket "dims"/"dimensions" covers all three;
    the canonical field name covers itself; "thickness" is the raw alias of the canonical `width` (depth)."""
    failed = set(row.fetch_failed_fields or ())
    if not failed:
        return False
    return ("dims" in failed or "dimensions" in failed or field in failed
            or (field == "width" and "thickness" in failed))


def _resolve_dimension(row: CanonicalRow, field: str, value: float | None, default: float,
                       category: str, methods: list[str]) -> float | None:
    """Keep a real parsed value (INCLUDING 0, which validate then rejects as a data error). A value missing
    because its FETCH FAILED is HELD (left None + flagged dimension_unavailable) so the row retries next
    scrape -- never defaulted, so freight is never computed from a fabricated size. A GENUINELY-absent value
    is filled from the pack default, flagged. Never fills over a real value."""
    if value is not None:
        methods.append(f"{field}:parsed")
        return value
    if _dim_fetch_failed(row, field):
        methods.append(f"{field}:unavailable")
        row.add_flag(ReviewFlag(field=field, code=FlagCode.dimension_unavailable,
                                confidence=Confidence.none, method="fetch_failed", src_url=row.src_url))
        return None
    methods.append(f"{field}:defaulted")
    row.add_flag(ReviewFlag(field=field, code=FlagCode.dimension_defaulted,
                            best_guess=f"{default}m ({category} standard)",
                            confidence=Confidence.low, method="pack_default", src_url=row.src_url))
    return default


def _normalize_unit(value: float | None, field: str, category: str) -> tuple[float | None, bool]:
    """General unit sanity for one linear dimension, in metres. A stone dimension stored in CENTIMETRES
    (a real SlabWare data inconsistency -- a face recorded as 332 rather than 3.32) reads ~100x too large in
    metres and blows up the freight weight. Correct it once, against the SAME per-category physical range the
    range check uses: a value far above the plausible max (> hi*3) whose value/100 lands back in the plausible
    band [lo*0.3, hi*3] was centimetres -> divide by 100. This is deterministic and source-agnostic, and by
    construction it can ONLY touch a value that is already physically impossible as metres, so a correct value
    is never changed (2.8 stays 2.8; a genuinely odd 6.44 whose /100 is implausibly small is left for review).
    Returns (value, corrected)."""
    if value is None:
        return value, False
    lo, hi = active_pack().dimension_ranges.get(category, {}).get(field, (0.0, 5.0))
    if value > hi * 3 and lo * 0.3 <= value / 100.0 <= hi * 3:
        return round(value / 100.0, 3), True
    return value, False


def derive_dimensions(row: CanonicalRow, ref: ReferenceData) -> None:
    """Resolve length/width/height in metres. length+height are the two FACE dimensions (a range takes its
    MAX); width is the depth/thickness. Each parses from the scraped strings; anything still missing or
    ambiguous (MULTI thickness, 'Free' length, absent) is filled from the pack's dimension_defaults for the
    row's category and provenance-flagged -- never silent, never over a real value. A parsed 0 stays 0, so
    validate rejects it (a wrong real size breaks area/volume pricing + freight)."""
    category = _dimension_category(row)
    defaults = active_pack().dimension_defaults[category]

    faces: dict[str, float] = {}
    if row.raw_dimensions:
        for part in row.raw_dimensions.split(";"):
            if "=" not in part:
                continue
            key, val = part.split("=", 1)
            key = key.strip().lower()
            meters = _parse_face(val, ref) if key in _FACE_DIMS else _parse_thickness(val, ref)
            if meters is not None:
                faces[key] = meters

    # thickness: the row's own field first, then a width=/thickness= entry in the dims string.
    width = _parse_thickness(row.raw_thickness, ref) if row.raw_thickness else None
    if width is None and not row.raw_thickness:
        width = faces.get("width", faces.get("thickness"))

    methods: list[str] = []
    length = _resolve_dimension(row, "length", faces.get("length"), defaults["length"], category, methods)
    height = _resolve_dimension(row, "height", faces.get("height"), defaults["height"], category, methods)
    width = _resolve_dimension(row, "width", width, defaults["thickness"], category, methods)

    # Unit sanity BEFORE weight: a dimension stored in centimetres reads ~100x too large in metres and would
    # inflate the freight weight to thousands of tonnes. Correct it generally (every source, every dimension)
    # against the category's physical range; never touches a value that is valid as metres. See _normalize_unit.
    length, _lc = _normalize_unit(length, "length", category)
    height, _hc = _normalize_unit(height, "height", category)
    width, _wc = _normalize_unit(width, "width", category)
    for _name, _corr, _val in (("length", _lc, length), ("height", _hc, height), ("width", _wc, width)):
        if _corr:
            methods.append(f"{_name}:cm_to_m")
            row.add_flag(ReviewFlag(field=_name, code=FlagCode.dimension_unit_corrected,
                                    best_guess=f"{_val}m (read as centimetres)",
                                    confidence=Confidence.medium, method="cm_to_m", src_url=row.src_url))

    weight, weight_method = _derive_weight(row, ref, length, width, height)
    methods.append(weight_method)

    # range sanity: flag a parsed/derived value wildly outside the category's expected band.
    ranges = active_pack().dimension_ranges[category]
    for name, value in (("length", length), ("width", width), ("height", height), ("weight", weight)):
        if value is None:
            continue
        lo, hi = ranges.get(name, (0.0, 5.0))
        if value < lo * 0.3 or value > hi * 3:
            row.add_flag(ReviewFlag(field=name, code=FlagCode.dimension_out_of_range,
                                    raw_value=str(value), confidence=Confidence.low,
                                    method="range_check", src_url=row.src_url))

    row.length = round(length, 3) if length is not None else None
    row.width = round(width, 3) if width is not None else None
    row.height = round(height, 3) if height is not None else None
    row.weight = round(weight, 3) if weight is not None else None
    row.dimension_method = ",".join(methods)


# --- bundle size ladder (section 10.1, the exemplar) --------------------------
def derive_bundle_size(row: CanonicalRow, ref: ReferenceData, source_cfg: SourceConfig) -> None:
    if row.is_block:
        row.sold_in_bundle = False
        row.bundle_size = None
        row.bundle_size_method = "block_short_circuit"
        row.bundle_size_confidence = _conf_name(Confidence.high)
        return

    row.sold_in_bundle = True

    def _accept(value, method, conf, flag_code=None):
        row.bundle_size = int(value)
        row.bundle_size_method = method
        row.bundle_size_confidence = _conf_name(conf)
        if flag_code:
            row.add_flag(ReviewFlag(field="bundle_size", code=flag_code, best_guess=str(value),
                                    confidence=conf, method=method, src_url=row.src_url))

    # 2. explicit bundle size (a literal '0' is not a real count -- fall through)
    if (n := _pos_int(row.raw_bundle_size)) is not None:
        return _accept(n, "explicit_bundle_size", Confidence.high)
    # 3. explicit slab count (likewise reject a non-positive count)
    if (n := _pos_int(row.raw_slab_count)) is not None:
        return _accept(n, "explicit_slab_count", Confidence.high)
    # 4. slabs array length
    if row.raw_slabs_array:
        count = _count_slab_entries(row.raw_slabs_array)
        if count > 0:
            return _accept(count, "slabs_array_length", Confidence.high)
    # 5. area division
    total = _to_float(row.raw_total_m2)
    per = _to_float(row.raw_per_slab_m2)
    if total and per and per > 0:
        quotient = total / per
        if 1 <= quotient <= 60 and abs(quotient - round(quotient)) < 0.15:
            return _accept(round(quotient), "area_division", Confidence.medium)
        row.add_flag(ReviewFlag(field="bundle_size", code=FlagCode.bundle_ratio_noninteger,
                                best_guess=f"{quotient:.2f}", confidence=Confidence.low,
                                method="area_division", src_url=row.src_url))
    # 6. standard slab area
    if total:
        area = _standard_area(row.type_name)
        if area:
            est = max(1, round(total / area))
            return _accept(est, "standard_slab_area", Confidence.low, FlagCode.bundle_estimated)
    # 7. config default
    _accept(source_cfg.default_bundle_size, "config_default", Confidence.low, FlagCode.bundle_default)


# Medusa integer sanity ceiling: a parsed 1e20 must never ship as stock (import reject / overflow).
_INVENTORY_MAX = 1_000_000


def _inventory_from_area(row: CanonicalRow) -> int | None:
    """Stock as a PIECE count derived from an available stock AREA: pieces = raw_stock_m2 / one piece's face
    area (length x height, both metres, set by derive_dimensions which runs first). For a source that
    publishes square-metres in stock instead of a slab count (e.g. marenostone "Ready Stock"). Reads
    raw_stock_m2 -- NOT raw_total_m2, which is a bundle's own area and feeds bundle_size. Returns None when
    the stock area or the face dimensions are missing/non-positive (no derivation -> caller marks stock
    undetermined). A real 0 m2 is a trusted out-of-stock (0 pieces). A positive area floors to at LEAST 1
    piece: a partial piece is still sellable, so a genuinely-stocked product never floors to a false 0."""
    total = _to_float(row.raw_stock_m2)
    if total is None:
        return None
    if total <= 0:
        return 0
    length, height = row.length, row.height
    if not length or not height or length <= 0 or height <= 0:
        return None
    return max(1, int(total / (length * height)))


# Positive availability WORDS a supplier may publish instead of a count. Cross-source: read from the same
# raw stock fields the numeric ladder uses, so no per-source code. NOT a fabricated number -- the supplier
# asserts the item IS available, we just have no count, so derive_inventory seeds a made-to-order fallback.
_IN_STOCK_WORDS = frozenset({"unlimited", "limited", "in stock", "instock", "in-stock",
                             "available", "made to order", "made-to-order"})


def _in_stock_word(row: CanonicalRow) -> bool:
    """True when the scrape flags availability as one of the recognized positive WORDS (not a count) -- either
    in a free-text stock field or in a source's STRUCTURED availability flag (raw_stock_status, e.g. a
    WooCommerce "in-stock"). A negative flag ("out-of-stock") is not in the set, so a sold-out item never
    matches and is never given fabricated stock."""
    for value in (row.raw_stock_m2, row.raw_inventory_quantity, row.raw_stock_status):
        if " ".join((value or "").strip().lower().split()) in _IN_STOCK_WORDS:
            return True
    return False


# Definitive NEGATIVE availability a supplier may publish (structured flag or word). Cross-source: read from
# the same raw fields as the positive check. An explicit out-of-stock is the supplier stating the item is
# unavailable -- a TRUSTED sold-out, exactly like a literal count of 0.
_OUT_OF_STOCK_WORDS = frozenset({"out of stock", "outofstock", "out-of-stock",
                                 "sold out", "soldout", "sold-out", "unavailable", "discontinued"})


def _out_of_stock_flag(row: CanonicalRow) -> bool:
    """True when the scrape EXPLICITLY flags the item as unavailable (structured status or word). An unknown
    or missing status is NOT out of stock -- it returns False and the row falls through to the magnitude."""
    for value in (row.raw_stock_status, row.raw_stock_m2, row.raw_inventory_quantity):
        if " ".join((value or "").strip().lower().split()) in _OUT_OF_STOCK_WORDS:
            return True
    return False


def derive_inventory(row: CanonicalRow) -> None:
    """Stock level (units available), derived ONCE from real signals -- SEPARATE from bundle_size (the
    slabs-per-bundle multiplier); bundle_size is NEVER a stock source. Trust ladder, in order:
      0. an EXPLICIT out-of-stock flag (structured status / word) -> quantity 0, a trusted sold-out. This
         wins over a published stock AREA below: if the supplier says the item is unavailable it is
         unavailable, and a stale Ready-Stock figure must not resurrect it.
      1. an explicit count (raw_slab_count, then raw_inventory_quantity), a deliberate magnitude incl. 0.
      2. a count DERIVED from an available stock AREA (raw_stock_m2 / per-piece face area).
      3. a positive availability WORD/flag with no magnitude -> the made-to-order fallback qty.
    A parsed value INCLUDING 0 is a TRUSTED out-of-stock and ships as sold-out. Only when NO signal is present
    AND none can be derived is stock UNDETERMINED: left None + flagged stock_undetermined so validate HOLDS
    the row -- a missing measurement must never ship as a fabricated 0. Mirrors derive_dimensions' fill-but-
    flag contract (determined value incl. 0 ships; missing-and-underivable holds)."""
    # 0. an explicit out-of-stock flag is a trusted sold-out (quantity 0), authoritative over a stale area.
    if _out_of_stock_flag(row):
        row.inventory_quantity = 0
        row.inventory_method = "out_of_stock_flag"
        row.inventory_confidence = _conf_name(Confidence.high)
        return
    for candidate, method in ((row.raw_slab_count, "raw_slab_count"),
                              (row.raw_inventory_quantity, "raw_inventory_quantity")):
        text = str(candidate).strip() if candidate is not None else ""
        if not text:
            continue
        n = parse_number(text)
        if n is None or int(n) < 0:
            continue                                   # unparseable/negative here -> try the next signal, then area
        row.inventory_quantity = min(int(n), _INVENTORY_MAX)
        row.inventory_method = method
        row.inventory_confidence = _conf_name(Confidence.high)
        return
    pieces = _inventory_from_area(row)
    if pieces is not None:
        row.inventory_quantity = min(pieces, _INVENTORY_MAX)
        row.inventory_method = "stock_area_division"
        row.inventory_confidence = _conf_name(Confidence.medium)
        return
    # Qualitative availability: a supplier that flags stock as a WORD ('Unlimited', 'In Stock', 'Made to
    # Order') with no count. A real positive signal, so seed the made-to-order fallback and LIST (flagged
    # low-confidence estimate) instead of holding -- distinct from a genuinely-absent stock, which still
    # HOLDS below rather than shipping a fabricated number. The fallback COUNT is per category (a block is a
    # single large piece; a slab/tile is stocked in modest quantity), not one global magnitude.
    if _in_stock_word(row):
        row.inventory_quantity = active_pack().in_stock_fallback_qty[_dimension_category(row)]
        row.inventory_method = "in_stock_word_fallback"
        row.inventory_confidence = _conf_name(Confidence.low)
        return
    raw_present = any(str(c).strip() for c in (row.raw_slab_count, row.raw_inventory_quantity, row.raw_total_m2)
                      if c is not None)
    reason = "a stock signal was present but unusable" if raw_present else "no stock signal in the scrape"
    row.inventory_quantity = None
    row.inventory_method = "undetermined"
    row.inventory_confidence = _conf_name(Confidence.none)
    row.add_flag(ReviewFlag(field="inventory", code=FlagCode.stock_undetermined,
                            detail=f"stock could not be determined or derived ({reason})",
                            confidence=Confidence.none, method="undetermined", src_url=row.src_url))


@functools.lru_cache(maxsize=1)
def _standard_areas() -> tuple[float | None, dict[str, float]]:
    """Parse standard_slab_area.csv once: (default area, {type_casefold: area})."""
    path = SETTINGS.paths.standard_slab_area_csv
    if not path.exists():
        return None, {}
    default, by_type = None, {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for r in csv.DictReader(handle):
            if r["scope"] == "default":
                default = float(r["slab_area_m2"])
            elif r["scope"] == "type":
                by_type[r["key"].casefold()] = float(r["slab_area_m2"])
    return default, by_type


def _standard_area(type_name: str | None) -> float | None:
    default, by_type = _standard_areas()
    return by_type.get((type_name or "").casefold(), default)


@functools.lru_cache(maxsize=1)
def _type_densities() -> tuple[float, dict[str, float]]:
    """Parse type_density.csv once: (default density, {type_casefold: density kg/m3}). The CSV's own
    'default' row covers a type not listed. When the file is absent, the fallback is the ACTIVE PACK's
    default_density (stone: 2700; a wood/lime pack supplies its own), never a hardcoded stone value."""
    path = SETTINGS.paths.type_density_csv
    default, by_type = active_pack().default_density, {}
    if not path.exists():
        return default, by_type
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for r in csv.DictReader(handle):
            value = float(r["density_kg_m3"])
            if r["scope"] == "default":
                default = value
            elif r["scope"] == "type":
                by_type[r["key"].casefold()] = value
    return default, by_type


def _type_density(type_name: str | None) -> float:
    """Material density (kg/m3) for a stone type, defaulting to Marble for an unlisted type."""
    default, by_type = _type_densities()
    return by_type.get((type_name or "").casefold(), default)


def _to_float(text: str | None) -> float | None:
    return parse_number(text) if text else None


def _pos_int(raw) -> int | None:
    """A strictly-positive integer, or None. str.isdigit() is UNSAFE as an int() gate: it is True for
    Unicode digits and superscripts ('2'-superscript, '3'-superscript) that int() then rejects with
    ValueError. A scraped 'm2'/dimension fragment leaking into a count field therefore crashed the whole
    source batch (violating fail-loud-and-ISOLATED: one bad value must flag its row, never kill the run).
    int() is the real predicate; a non-positive or unparseable value simply falls through to the next
    strategy, exactly as a missing value does."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


# --- origin (section 7 Stage 6) -----------------------------------------------
def _to_iso(value: str, ref: ReferenceData) -> str | None:
    """Resolve a country NAME or alias ('India', 'UK'->GB) via country_codes FIRST, then accept a
    bare 2-letter token only if it is a real ISO-3166 alpha-2 code. Name-first so a common alias like
    'UK' maps to GB instead of short-circuiting to the (invalid) literal 'UK'; the ISO-set check
    rejects a bogus 'XX'/'EN' rather than passing it through as a confident country."""
    v = (value or "").strip()
    if not v:
        return None
    hit = ref.country_codes.get(match_key(v))   # same key the country table was built with (loaders._norm)
    if hit:
        return hit
    if len(v) == 2 and v.isalpha() and v.upper() in ref.valid_iso_codes:
        return v.upper()
    return None


def _select_origin(candidates: list[str], home_iso: str) -> tuple[str, Confidence, FlagCode | None]:
    """Pick ONE origin from a variety's candidate countries, given the supplier's home country:
      * one candidate               -> it (the variety has a single known origin).                  [rule c]
      * many + home is a candidate  -> home (a supplier sells its own country's quarry output).      [rule b]
      * many + home not a candidate -> first candidate, LOW confidence + review flag: the stone is   [rule d]
        quarried in several countries, none the supplier's, so we cannot infer which (the list is not
        priority-ordered) -- an operator resolves it (new origin in the list, or a per-supplier override).
    """
    if len(candidates) == 1:
        return candidates[0], Confidence.high, None
    if home_iso and home_iso in candidates:
        return home_iso, Confidence.high, None
    return candidates[0], Confidence.low, FlagCode.origin_multi_home_absent


def derive_origin(row: CanonicalRow, ref: ReferenceData, source_cfg: SourceConfig) -> None:
    # 1. scraped country: an ISO code OR a country name (the real data when present)
    if row.raw_origin:
        iso = _to_iso(row.raw_origin, ref)
        if iso:
            row.origin_country_code = iso
            row.origin_source = "scrape_field"
            row.origin_confidence = _conf_name(Confidence.high)
            return
    name = row.variation_name or row.raw_name or ""
    # The curated (name, type) lookups below are strict and only SAFE when the row's type is
    # variation-AUTHORITATIVE (from the variation's Key). A name-derived / fallback type would resolve a
    # HOMONYM's WRONG origin ('Azul White' is a quartzite in one supplier, an onyx in another) and stamp it
    # confidently with no flag. When the type is not authoritative, SKIP the curated lookups, flag the row
    # to verify its variety type, and fall to the supplier default.
    type_authoritative = row.type_method == "variety_authoritative"
    if not type_authoritative:
        row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_type_unverified,
                                raw_value=row.type_name or "", confidence=Confidence.low,
                                method=row.type_method or "type_unresolved", src_url=row.src_url))
    # 2. per-SUPPLIER override (source, variety, type) -> country: a curated decision for a supplier that
    #    sells a stone it does NOT quarry at home (a reseller). Highest curated truth; scoped to one source,
    #    so it never affects another supplier of the same variety.
    override = ref.origin_overrides.lookup(row.src_site, name, row.type_name) if type_authoritative else None
    if override:
        row.origin_country_code = override
        row.origin_source = "supplier_override"
        row.origin_confidence = _conf_name(Confidence.high)
        return
    # 2b. PER-VENDOR ORIGIN GATE (opt-in via primary_origin). A vendor that declares its own primary quarry
    #     country does NOT inherit a per-variety map origin blindly: an origin is trusted only when the map
    #     CORROBORATES the vendor -- the vendor's primary country is ONE OF the variety's documented origins
    #     (the vendor sources from a real quarry country for this stone). If it is, use it. If it is NOT -- the
    #     variety's map lists other countries but not this vendor's, or has no row -- the origin is NOT known
    #     for THIS vendor, so the row is held for a one-time origin CONFIRMATION (the separate origin review
    #     queue) rather than silently shipping the map's guess. Operators widen a variety's origins (the "edit
    #     origins" action) to add a vendor's real country; a confirmed answer returns above as a
    #     supplier_override. Opt-in: an unset primary_origin runs the classic ladder below, every other vendor
    #     untouched.
    if source_cfg.primary_origin and type_authoritative:
        vendor_iso = _to_iso(source_cfg.primary_origin, ref)
        rule = ref.origin_map.exact(name, row.type_name) if vendor_iso else None
        if rule and vendor_iso in rule.countries:
            row.origin_country_code = vendor_iso
            row.origin_city = rule.city
            row.origin_county = rule.county
            row.origin_source = "vendor_origin"
            row.origin_confidence = _conf_name(Confidence.high)
            return
        # Not corroborated -> leave origin UNRESOLVED so validate holds the product (never a Medusa-breaking
        # blank shipped, never a silent guess), and flag it for the origin review queue.
        row.origin_source = "origin_needs_confirmation"
        row.origin_confidence = _conf_name(Confidence.none)
        row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_needs_confirmation,
                                raw_value=",".join(rule.countries) if rule else "", best_guess=vendor_iso,
                                confidence=Confidence.none, method="vendor_map_mismatch", src_url=row.src_url))
        return
    # 3. the per-variety origin map = curated origin_map.csv + operator-minted overlay (seed_country).
    #    Strict (name, type) match; the map is the source of truth. A row may list SEVERAL candidate
    #    countries (same trade name quarried in more than one place) -- pick the one that fits the
    #    supplier's home country (see _select_origin), never a country guessed from another type.
    rule = ref.origin_map.exact(name, row.type_name) if type_authoritative else None
    if rule and rule.countries:
        home = _to_iso(source_cfg.origin_default, ref) or ""
        iso, conf, flag = _select_origin(rule.countries, home)
        row.origin_country_code = iso
        row.origin_city = rule.city
        row.origin_county = rule.county
        row.origin_source = "origin_map"
        row.origin_confidence = _conf_name(conf)
        if flag:
            row.add_flag(ReviewFlag(field="origin", code=flag, raw_value=",".join(rule.countries),
                                    best_guess=iso, confidence=conf, method="multi_home_absent",
                                    src_url=row.src_url))
        return
    # 4. supplier-country fallback. Strictly, the supplier's country is where the stone is SOLD FROM,
    #    not necessarily where it was quarried (a trader can ship stone from many countries) -- so the
    #    accurate origin is the scraped field or origin_map above. But Medusa REQUIRES an origin for
    #    its pricing-rule lookup, so a blank breaks the import. We stamp the supplier's default country
    #    as a LOW-confidence fallback and flag it for review, which both lets the product import and
    #    queues it so origin_map gets expanded with the real per-variety origin.
    iso = _to_iso(source_cfg.origin_default, ref)
    if iso:
        row.origin_country_code = iso
        row.origin_source = "supplier_default"
        row.origin_confidence = _conf_name(Confidence.low)
        row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_supplier_default,
                                raw_value=row.raw_origin, best_guess=iso, confidence=Confidence.low,
                                method="supplier_default", src_url=row.src_url))
        return
    # 5. no scrape, no override, no map, no supplier default: leave UNRESOLVED and flag. The Process gate
    #    rejects such a row (origin is required downstream) rather than emit a Medusa-breaking blank.
    row.origin_source = "unresolved"
    row.origin_confidence = _conf_name(Confidence.none)
    row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_unresolved,
                            raw_value=row.raw_origin, confidence=Confidence.none,
                            method="no_strategy", src_url=row.src_url))


# --- ports (section 3.3) ------------------------------------------------------
def derive_ports(row: CanonicalRow, ref: ReferenceData, source_cfg: SourceConfig) -> None:
    # ports belong to the SUPPLIER (the scraped website's company) and where it ships
    # from -- set per source in sources.yaml by name/locode/id, resolved against ports.csv.
    # Independent of the stone's origin.
    row.port_ids = [pid for t in (source_cfg.ports_default or []) if (pid := ref.ports.resolve(t))]
    if source_cfg.ports_default and not row.port_ids:
        row.add_flag(ReviewFlag(field="port_ids", code=FlagCode.ports_default,
                                raw_value="|".join(source_cfg.ports_default),
                                confidence=Confidence.low, method="port_unresolved",
                                src_url=row.src_url))


# --- collection location (section 3.3b) ---------------------------------------
def derive_collection(row: CanonicalRow, ref: ReferenceData, source_cfg: SourceConfig) -> None:
    # Where this source's products are physically COLLECTED before shipping (the supplier's warehouse): a
    # per-source constant, independent of origin (provenance/quarry) and of ports (departure). Optional and
    # NEVER required -- a blank config leaves both null and the product still lists. Country is validated
    # against the same ISO reference as origin (a typo emits null + a loud log, never a wrong country); city
    # is free text. City is only meaningful WITH a resolved country (the emit contract), so the two stand or
    # fall together. A per-PRODUCT scrape override can layer here later (precedence scrape > default).
    country = (source_cfg.collection_country_default or "").strip()
    city = (source_cfg.collection_city_default or "").strip()
    iso = _to_iso(country, ref) if country else None
    if country and not iso:
        log.warning("collection_country_default did not resolve to a country; emitting null collection",
                    extra={"extra_fields": {"source": source_cfg.source, "value": country}})
    elif city and not country:
        log.warning("collection_city set without a collection_country_default; emitting null collection "
                    "(a city is only meaningful with a country)",
                    extra={"extra_fields": {"source": source_cfg.source, "city": city}})
    row.collection_country_code = iso
    row.collection_city = (city or None) if iso else None


# --- title / description / handle (section 10.3, 10.4) ------------------------
_FORMAT_WORD = {c.name: c.name.title() for c in CATEGORIES}  # slab -> Slab


def _primary_variety_name(name: str) -> str:
    """Drop a trailing parenthetical alias from a variant name for display
    (e.g. 'Carrara (Bianco Carrara)' -> 'Carrara')."""
    return re.sub(r"\s*\(.*?\)\s*", " ", name or "").strip()


def _material_suffix(variety_name: str, type_name: str | None) -> str:
    """The stone type to append to a title, or '' when the variety name already implies it.
    Skips if ANY non-generic token of the type is already a whole word in the variety name, so
    'Walnut Travertine' + Travertine, or 'X Sandstone' + Quartzitic Sandstone, never doubles the
    material. 'stone' is treated as generic (it appears in many names + multi-word types). Biases
    to skip: a missing material only costs a little findability, a doubled one reads broken."""
    if not type_name:
        return ""
    generic_tokens = {active_pack().generic_material_word}   # the domain's generic material noun ('stone')
    type_tokens = [t for t in re.findall(r"[a-z]+", type_name.casefold()) if t not in generic_tokens]
    if not type_tokens:
        return ""                                  # a purely generic type ('Stone') adds nothing
    variety_tokens = set(re.findall(r"[a-z]+", (variety_name or "").casefold()))
    if any(t in variety_tokens for t in type_tokens):
        return ""                                  # the variety name already names this type
    return type_name


def derive_title(row: CanonicalRow) -> None:
    # A listing-style title: variety, then finish and material when they add information. The FORMAT word
    # (Slab/Block/Tile) is deliberately NOT in the title -- format is a variant dimension in Medusa, not
    # product identity, so 'Cream Marble Polished', never 'Cream Marble Polished Tile' (the format word
    # stays only in the description, via _FORMAT_WORD). Finish is dropped for blocks (uncut stone has no
    # finish; avoids 'Carrara Raw'); material is omitted when the variety name already implies it
    # (_material_suffix). The handle keys off variety+finish INDEPENDENTLY (derive_handle).
    if not row.variation_name:
        row.title = title_case(row.raw_name or "")
        row.title_method = "raw_name_fallback"
        return
    variety = _primary_variety_name(row.variation_name)
    parts = [variety]
    if row.finish_name and not row.is_block:
        parts.append(row.finish_name)
    if material := _material_suffix(variety, row.type_name):
        parts.append(material)
    row.title = title_case(" ".join(p for p in parts if p))
    row.title_method = "construct"


def derive_description(row: CanonicalRow) -> None:
    if row.raw_description and len(row.raw_description.strip()) > 20:
        row.description = row.raw_description.strip()
        row.description_method = "passthrough"
        return
    variety = title_case(_primary_variety_name(row.variation_name or row.raw_name or "This stone"))
    stone_type = (row.type_name or active_pack().generic_material_word).lower()  # generic material noun
    color = (row.color_name or "").lower()               # OMIT when unresolved -- never invent 'natural'
    material = f"{color} {stone_type}".strip()           # 'black marble', or just 'marble'
    # origin is a real provenance claim -- only stated when both city and country resolved, else omitted
    # (never a generic 'natural stone' filler that reads redundant next to the material).
    origin_clause = f" from {row.origin_city}, {row.origin_country_code}" \
        if row.origin_city and row.origin_country_code else ""
    # the RESOLVED format word (format_resolve ran already); a neutral 'piece' beats mislabelling a
    # block as a 'slab' when the format is genuinely unresolved.
    fmt = _FORMAT_WORD.get((row.format_value or "").strip().casefold(), "").lower() or "piece"
    finish = (row.finish_name or "").lower()
    _pack = active_pack()
    phrase = _pack.finish_phrases.get(finish, _pack.finish_phrase_default)
    finish_clause = f"a {finish} {fmt}" if finish else f"a {fmt}"
    # real thickness (slabs/tiles): width IS the parsed thickness in metres, never fabricated. Omit for
    # blocks and when missing or outside a sane display band (a mis-scaled parse validate will reject).
    thickness_clause = ""
    if not row.is_block and row.width is not None:
        mm = round(row.width * 1000)
        if 3 <= mm <= 80:
            thickness_clause = f" at {mm} mm"
    row.description = (
        f"{variety} is a {material}{origin_clause}. "
        f"Supplied as {finish_clause}{thickness_clause}, it offers {phrase}."
    )
    row.description_method = "template"


def derive_handle(row: CanonicalRow, source_cfg: SourceConfig) -> None:
    # The handle/slug is the product's stable URL, so key it off variety+finish (the product's core
    # identity), NOT the enriched display title -- adding material/format to the title must never
    # move a URL. This reproduces the pre-enrichment handle byte-for-byte (the old title WAS
    # variety+finish, block finish included), so no existing URL churns. Namespaced with source +
    # surrogate for global uniqueness and stable upsert (section 10 Stage 10, section 11.4).
    if row.variation_name:
        parts = [_primary_variety_name(row.variation_name)]
        if row.finish_name:                        # finish always in the URL base (incl. block 'Raw')
            parts.append(row.finish_name)
        base = slugify(" ".join(p for p in parts if p))
    else:
        base = slugify(row.raw_name or "")
    namespaced = f"{base}-{source_cfg.source_code}-{slugify(row.surrogate_key or '')}"
    row.handle = namespaced
    row.slug = namespaced


def _apply_overrides(row: CanonicalRow, ref: ReferenceData) -> None:
    """Override is the top strategy for the derived fields too (section 8.4)."""
    if ref.overrides is None:
        return
    def get(f):
        return ref.overrides.get(row.src_site, row.surrogate_key or "", f)
    if (n := _pos_int(get("bundle_size"))) is not None:
        row.bundle_size, row.bundle_size_method, row.bundle_size_confidence = n, "override", "high"
    if v := get("origin_country_code"):
        iso = _to_iso(v, ref)   # validate the override to ISO-2 like a scraped value; don't stamp 'BRASIL'
        if iso:
            row.origin_country_code, row.origin_source, row.origin_confidence = iso, "override", "high"
            # the derived city/county belonged to the OLD country -- drop them so an override can't leave
            # e.g. country=BR with city='Carrara'; an explicit origin_city below re-sets it if intended.
            row.origin_city = row.origin_county = ""
        else:
            log.warning("override origin_country_code did not resolve to a country; ignored",
                        extra={"extra_fields": {"value": v, "surrogate": row.surrogate_key}})
    if v := get("origin_city"):
        row.origin_city = v
    if v := get("title"):
        row.title, row.title_method = v, "override"
    if v := get("description"):
        row.description, row.description_method = v, "override"


def _provenance_gap(row: CanonicalRow) -> bool:
    """A derived value present WITHOUT its provenance method is a bug (invariant: provenance on every
    derived value). Check the load-bearing derived fields; a gap here should never happen."""
    pairs = ((row.width or row.length or row.height, row.dimension_method),
             (row.origin_country_code, row.origin_source),
             (row.title, row.title_method),
             (row.bundle_size, row.bundle_size_method))
    return any(value and not method for value, method in pairs)


def _derive_row(row: CanonicalRow, ref: ReferenceData, source_cfg: SourceConfig) -> None:
    derive_category(row, ref)
    derive_dimensions(row, ref)
    derive_bundle_size(row, ref, source_cfg)
    derive_inventory(row)                          # stock, once, from raw signals only (never bundle_size)
    derive_origin(row, ref, source_cfg)
    derive_ports(row, ref, source_cfg)
    derive_collection(row, ref, source_cfg)
    derive_title(row)
    derive_description(row)
    _apply_overrides(row, ref)  # overrides win over the derived values
    derive_handle(row, source_cfg)  # handle follows the (possibly overridden) title


def run(rows: list[CanonicalRow], ref: ReferenceData, source_cfg: SourceConfig) -> StageMetric:
    isolated = isolate_rows(rows, "derive", lambda r: _derive_row(r, ref, source_cfg), log)
    # A dimension filled from the pack default (always flagged, never silent) means the source did not ship
    # a real size for that row; a high share signals a source that stopped shipping sizes. A provenance gap
    # is an invariant breach. Either way -> DEGRADED (the per-row default still emits, but the batch is flagged).
    incomplete = sum(1 for r in rows
                     if any(f.code == FlagCode.dimension_defaulted for f in r.review_flags))
    # a fetch-failed dimension is HELD (validate rejects it), never defaulted; a high share signals a source
    # under rate-limiting/outage this run. Counted separately (it is recoverable, not a source gap) but also
    # trips DEGRADED so a systemic fetch failure is loud.
    unavailable = sum(1 for r in rows
                      if any(f.code == FlagCode.dimension_unavailable for f in r.review_flags))
    provenance_gaps = sum(1 for r in rows if _provenance_gap(r))
    thr = SETTINGS.thresholds
    degraded = (rows and ((incomplete + unavailable) / len(rows) >= thr.derive_incomplete_degraded
                          or isolated / len(rows) >= thr.row_isolated_degraded)) \
        or provenance_gaps
    log.info("derive done", extra={"extra_fields": {
        "rows": len(rows), "incomplete_dims": incomplete, "unavailable_dims": unavailable,
        "isolated": isolated}})
    return StageMetric(stage="derive", status=DEGRADED if degraded else OK, rows_in=len(rows), rows_out=len(rows),
                       extra={"incomplete_dims": incomplete, "unavailable_dims": unavailable,
                              "provenance_gaps": provenance_gaps, "isolated": isolated})
