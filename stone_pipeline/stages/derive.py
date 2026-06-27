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
import re
from dataclasses import dataclass

from stone_pipeline.config.settings import CATEGORIES, SETTINGS, Confidence
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import ids, logfmt
from stone_pipeline.core.numbers import normalize_unit, parse_number
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.core.text import slugify, title_case
from stone_pipeline.reference.loaders import ReferenceData

log = logfmt.get_logger("derive")

_NUM_UNIT = re.compile(r"(-?\d[\d.,]*\d|-?\d)\s*([a-zµ\"'″′’”]+)?", flags=re.IGNORECASE)

# section 10.2 branch dimension/weight ranges in METRES/tonnes (fallback only; parsed dims win).
# A tile is a small finished piece (~30–60 cm face, ~1–2 cm thick), NOT a slab — sources often
# ship tiles with no dimensions, so the synthetic fill MUST be tile-sized or tiles come out slab-big.
_SLAB_RANGES = {"weight": (0.225, 0.350), "length": (1.5, 3.0), "width": (0.02, 0.03), "height": (1.5, 3.0)}
_BLOCK_RANGES = {"weight": (18.0, 23.0), "length": (1.5, 3.0), "width": (1.5, 3.0), "height": (1.5, 3.0)}
_TILE_RANGES = {"weight": (0.005, 0.012), "length": (0.3, 0.6), "width": (0.01, 0.02), "height": (0.3, 0.6)}

# section 10.4 finish-to-surface-phrase table
_FINISH_PHRASE = {
    "honed": "a smooth matte surface",
    "polished": "a bright reflective surface",
    "leathered": "a soft textured surface",
    "brushed": "a lightly textured surface",
    "flamed": "a rugged non-slip surface",
    "split face": "a dramatic three-dimensional natural cleft",
    "sandblasted": "a fine matte texture",
    "tumbled": "a worn antique surface",
}


def _conf_name(c: Confidence) -> str:
    return Confidence(c).name


def _parse_measure(text: str, ref: ReferenceData) -> float | None:
    """Parse a single '2cm' / '2.80m' measure to metres via units.csv."""
    if not text:
        return None
    match = _NUM_UNIT.search(text)
    if not match:
        return None
    value = parse_number(match.group(1))
    if value is None:
        return None
    raw_token = (match.group(2) or "").strip()
    token = normalize_unit(match.group(2))   # quote marks -> in/ft; empty -> m
    converted = ref.units.convert(value, token)
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
def derive_dimensions(row: CanonicalRow, ref: ReferenceData) -> None:
    parsed: dict[str, float] = {}
    if row.raw_dimensions:
        for part in row.raw_dimensions.split(";"):
            if "=" in part:
                key, val = part.split("=", 1)
                meters = _parse_measure(val, ref)
                if meters is not None:
                    parsed[key.strip().lower()] = meters
    width = _parse_measure(row.raw_thickness, ref) if row.raw_thickness else None
    if width is None:                         # fall back to a width=/thickness= entry in raw_dimensions
        width = parsed.get("width") or parsed.get("thickness")   # (was parsed but never read)

    fmt = (row.format_value or "").casefold()
    ranges = (_BLOCK_RANGES if (row.is_block or fmt == "block")
              else _TILE_RANGES if fmt == "tile" else _SLAB_RANGES)
    methods = []

    length = parsed.get("length")
    height = parsed.get("height")
    # Synthesise a SIZE only when the scrape gave NONE (absent). A value that was PRESENT but invalid
    # (<= 0, e.g. marenostone's "0cm" thickness typo) is deliberately LEFT <= 0 so validate REJECTS
    # the whole product -- we never fabricate over bad source data; the product is skipped instead.
    if length is None:
        length = round(ids.seeded_uniform(row.surrogate_key, "length", *ranges["length"]), 3)
        methods.append("length:synthetic")
    else:
        methods.append("length:parsed")
    if height is None:
        height = round(ids.seeded_uniform(row.surrogate_key, "height", *ranges["height"]), 3)
        methods.append("height:synthetic")
    else:
        methods.append("height:parsed")
    if width is None:
        low, high = ranges.get("width", (0.2, 0.2))
        width = round(ids.seeded_uniform(row.surrogate_key, "width", low, high), 3) if low != high else low
        methods.append("width:synthetic")
    else:
        methods.append("width:parsed")

    # units.csv converts weight to KILOGRAMS, but the synthetic ranges and the emitted
    # "Product Weight" are TONNES (a block is ~20 t, not 20 kg). Convert kg->t so a scraped
    # weight and a synthetic one are the same unit (else a source that supplies weight ships a
    # 1000x-too-large value). Dimensions stay in metres (this /1000 is weight-only).
    weight_kg = _parse_measure(row.raw_weight, ref) if row.raw_weight else None
    if weight_kg is not None and weight_kg > 0:
        weight = weight_kg / 1000.0
        methods.append("weight:parsed")
    else:
        weight = round(ids.seeded_uniform(row.surrogate_key, "weight", *ranges["weight"]), 3)
        methods.append("weight:synthetic")

    # range sanity: flag out-of-range parsed dims (section 6 Stage 6)
    for name, value in (("length", length), ("width", width), ("height", height)):
        lo, hi = ranges.get(name, (0.0, 5.0))
        if value < lo * 0.3 or value > hi * 3:
            row.add_flag(ReviewFlag(field=name, code=FlagCode.dimension_out_of_range,
                                    raw_value=str(value), confidence=Confidence.low,
                                    method="range_check", src_url=row.src_url))

    row.length, row.width, row.height, row.weight = round(length, 3), round(width, 3), round(height, 3), round(weight, 3)
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
    if row.raw_bundle_size and row.raw_bundle_size.strip().isdigit() and int(row.raw_bundle_size) > 0:
        return _accept(int(row.raw_bundle_size), "explicit_bundle_size", Confidence.high)
    # 3. explicit slab count (likewise reject a non-positive count)
    if row.raw_slab_count and row.raw_slab_count.strip().isdigit() and int(row.raw_slab_count) > 0:
        return _accept(int(row.raw_slab_count), "explicit_slab_count", Confidence.high)
    # 4. slabs array length
    if row.raw_slabs_array:
        count = row.raw_slabs_array.count('"n"') or row.raw_slabs_array.count('"Numero"')
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


def _to_float(text: str | None) -> float | None:
    return parse_number(text) if text else None


# --- origin (section 7 Stage 6) -----------------------------------------------
def _to_iso(value: str, ref: ReferenceData) -> str | None:
    """Resolve a country NAME or alias ('India', 'UK'->GB) via country_codes FIRST, then accept a
    bare 2-letter token only if it is a real ISO-3166 alpha-2 code. Name-first so a common alias like
    'UK' maps to GB instead of short-circuiting to the (invalid) literal 'UK'; the ISO-set check
    rejects a bogus 'XX'/'EN' rather than passing it through as a confident country."""
    v = (value or "").strip()
    if not v:
        return None
    hit = ref.country_codes.get(" ".join(v.casefold().split()))
    if hit:
        return hit
    if len(v) == 2 and v.isalpha() and v.upper() in ref.valid_iso_codes:
        return v.upper()
    return None


def derive_origin(row: CanonicalRow, ref: ReferenceData, source_cfg: SourceConfig) -> None:
    # 1. scraped country: an ISO code OR a country name (the real data when present)
    if row.raw_origin:
        iso = _to_iso(row.raw_origin, ref)
        if iso:
            row.origin_country_code = iso
            row.origin_source = "scrape_field"
            row.origin_confidence = _conf_name(Confidence.high)
            return
    # 2. origin_map — the per-variety override built from the master variety reference. Two tiers:
    #    an EXACT variety-name rule (trusted, the variety's known country), or a geographic name
    #    PATTERN (e.g. 'carrara'->IT, 'persa'->BR) that generalises to a variety the exact tier has
    #    never seen -- this is what carries origin onto a brand-new variant. A pattern hit is a strong
    #    data-driven guess, so it's still emitted at medium confidence but FLAGGED, so it surfaces for
    #    a one-time confirm; once confirmed into the raw reference it becomes an exact rule next build.
    rule = ref.origin_map.lookup(row.variation_name or row.raw_name or "")
    if rule:
        row.origin_country_code = rule.country_iso
        row.origin_city = rule.city
        row.origin_county = rule.county
        row.origin_confidence = _conf_name(Confidence.medium)
        if rule.match_type == "pattern":
            row.origin_source = "origin_pattern"
            row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_pattern_guess,
                                    raw_value=rule.pattern, best_guess=rule.country_iso,
                                    confidence=Confidence.medium, method="origin_pattern",
                                    src_url=row.src_url))
        else:
            row.origin_source = "origin_map"
        return
    # 3. supplier-country fallback. Strictly, the supplier's country is where the stone is SOLD FROM,
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
    # 4. no scrape, no map, no supplier default: leave UNRESOLVED and flag. The Process gate rejects
    #    such a row (origin is required downstream) rather than emit a Medusa-breaking blank.
    row.origin_source = "unresolved"
    row.origin_confidence = _conf_name(Confidence.none)
    row.add_flag(ReviewFlag(field="origin", code=FlagCode.origin_unresolved,
                            raw_value=row.raw_origin, confidence=Confidence.none,
                            method="no_strategy", src_url=row.src_url))


# --- ports (section 3.3) ------------------------------------------------------
def derive_ports(row: CanonicalRow, ref: ReferenceData, source_cfg: SourceConfig) -> None:
    # ports belong to the SUPPLIER (the scraped website's company) and where it ships
    # from — set per source in sources.yaml by name/locode/id, resolved against ports.csv.
    # Independent of the stone's origin.
    row.port_ids = [pid for t in (source_cfg.ports_default or []) if (pid := ref.ports.resolve(t))]
    if source_cfg.ports_default and not row.port_ids:
        row.add_flag(ReviewFlag(field="port_ids", code=FlagCode.ports_default,
                                raw_value="|".join(source_cfg.ports_default),
                                confidence=Confidence.low, method="port_unresolved",
                                src_url=row.src_url))


# --- title / description / handle (section 10.3, 10.4) ------------------------
_FORMAT_WORD = {c.name: c.name.title() for c in CATEGORIES}  # slab -> Slab


def _primary_variety_name(name: str) -> str:
    """Drop a trailing parenthetical alias from a variant name for display
    (e.g. 'Carrara (Bianco Carrara)' -> 'Carrara')."""
    return re.sub(r"\s*\(.*?\)\s*", " ", name or "").strip()


def derive_title(row: CanonicalRow) -> None:
    # variety (+ finish) only — NOT the category word (no 'Slab'/'Block'/'Tile' in the title)
    if row.variation_name:
        parts = [_primary_variety_name(row.variation_name)]
        if row.finish_name:
            parts.append(row.finish_name)
        row.title = title_case(" ".join(parts))
        row.title_method = "construct"
    else:
        row.title = title_case(row.raw_name or "")
        row.title_method = "raw_name_fallback"


def derive_description(row: CanonicalRow) -> None:
    if row.raw_description and len(row.raw_description.strip()) > 20:
        row.description = row.raw_description.strip()
        row.description_method = "passthrough"
        return
    variety = title_case(_primary_variety_name(row.variation_name or row.raw_name or "This stone"))
    color = (row.color_name or "natural").lower()
    stone_type = (row.type_name or "stone").lower()
    if row.origin_city and row.origin_country_code:
        origin_clause = f"extracted in {row.origin_city}, {row.origin_country_code}"
    else:
        origin_clause = "natural stone"
    fmt = _FORMAT_WORD.get((row.format_value or "").strip().casefold(), "slab").lower()
    finish = (row.finish_name or "").lower()
    phrase = _FINISH_PHRASE.get(finish, "a refined natural surface")
    finish_clause = f"a {finish} {fmt}" if finish else f"a {fmt}"
    row.description = (
        f"{variety} is a {color} {stone_type} {origin_clause}. "
        f"Supplied as {finish_clause}, it presents {phrase}."
    )
    row.description_method = "template"


def derive_handle(row: CanonicalRow, source_cfg: SourceConfig) -> None:
    # slugify(title), namespaced with source code + surrogate for global
    # uniqueness and stable upsert (section 10 Stage 10, section 11.4)
    base = slugify(row.title or row.raw_name or "")
    namespaced = f"{base}-{source_cfg.source_code}-{slugify(row.surrogate_key or '')}"
    row.handle = namespaced
    row.slug = namespaced


def _apply_overrides(row: CanonicalRow, ref: ReferenceData) -> None:
    """Override is the top strategy for the derived fields too (section 8.4)."""
    if ref.overrides is None:
        return
    get = lambda f: ref.overrides.get(row.src_site, row.surrogate_key or "", f)
    if (v := get("bundle_size")) and str(v).isdigit():
        row.bundle_size, row.bundle_size_method, row.bundle_size_confidence = int(v), "override", "high"
    if v := get("origin_country_code"):
        row.origin_country_code, row.origin_source, row.origin_confidence = v.upper(), "override", "high"
        # the derived city/county belonged to the OLD country -- drop them so an override can't leave
        # e.g. country=BR with city='Carrara'; an explicit origin_city below re-sets it if intended.
        row.origin_city = row.origin_county = ""
    if v := get("origin_city"):
        row.origin_city = v
    if v := get("title"):
        row.title, row.title_method = v, "override"
    if v := get("description"):
        row.description, row.description_method = v, "override"


@dataclass
class DeriveStats:
    rows: int = 0


def run(rows: list[CanonicalRow], ref: ReferenceData, source_cfg: SourceConfig) -> DeriveStats:
    for row in rows:
        derive_category(row, ref)
        derive_dimensions(row, ref)
        derive_bundle_size(row, ref, source_cfg)
        derive_origin(row, ref, source_cfg)
        derive_ports(row, ref, source_cfg)
        derive_title(row)
        derive_description(row)
        _apply_overrides(row, ref)  # overrides win over the derived values
        derive_handle(row, source_cfg)  # handle follows the (possibly overridden) title
    log.info("derive done", extra={"extra_fields": {"rows": len(rows)}})
    return DeriveStats(rows=len(rows))
