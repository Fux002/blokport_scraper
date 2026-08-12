"""Fuleistone adapter (section 7 Stage 1, section 14 M11).

A WooCommerce slab source (stone.fuleistone.com) whose stone facts arrive as pa_* attributes. Two source
shapes need a DECLARED collapse rule here (source isolation), because the canonical schema is single-valued
while fuleistone lists several terms per attribute:

  * Material / Color / Finish / Thickness are MULTI-VALUE. The declared collapse:
      - type   : the single material, or -- when several -- the one named in the product title; a value that
                 names no known Medusa type ('Agate Onyx', 'Terrazzo Stone', a multi-material with no title
                 match) yields '' and the row HOLDS for review (identity is never guessed).
      - color  : the first term that resolves to a real colour ('Gold / Yellow' -> Gold via extract_color);
                 the non-colour noise ('Multi-Color', 'Wooden') is skipped.
      - finish : the first surface finish, dropping 'Bookmatched' (a slab LAYOUT, not a finish); an unknown
                 finish ('Groove') passes through and takes the pack's flagged last-resort finish downstream.
      - thickness: the first real 'N mm' value (several offered thicknesses are listed).

  * The variety lives in the product NAME. Live-inventory lots wrap it as
    'Instock Slabs - <variety> [- Bookmatched] - <area>m2'; the rest are '<variety> <Material> Slab(s)'.
    generic_descriptor keeps colour+type tokens so a real variety resolves and a generic one routes to review.

The SKU is always blank; the stable WooCommerce product id is the natural key. Fuleistone publishes no quality
attribute, so quality takes the pack's flagged last-resort (A) downstream -- it never holds the row.
"""

from __future__ import annotations

from functools import lru_cache

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.adapters.tokens import extract_color, known_values, recognize_type, strip_format
from stone_pipeline.core.text import match_key


def _terms(value: str) -> list[str]:
    """The pipe-joined terms the scraper captured for a multi-value attribute, in source order."""
    return [t.strip() for t in (value or "").split("|") if t.strip()]


@lru_cache(maxsize=1)
def _finish_lookup() -> dict:
    """match_key -> canonical finish, from the live finish vocabulary + its synonyms (so 'Honed / Matte' and
    'Saw Cut' canonicalise). Mirrors the shared type/colour recognizers; one vocabulary, no hardcoding."""
    from stone_pipeline.reference.loaders import load_synonyms
    lut = {match_key(v): v for v in known_values("finish")}
    lut.update({match_key(raw): canon for raw, canon in load_synonyms("finish").items()})
    return lut


def _pick_type(material_raw: str, name: str) -> str:
    """One canonical stone type. A single material resolves directly; several resolve to the one named in the
    title. recognize_type returns '' for a value naming no known Medusa type, so an ambiguous or unknown
    material HOLDS for review instead of being guessed (identity is never fabricated)."""
    terms = _terms(material_raw)
    if len(terms) == 1:
        return recognize_type(terms[0])
    low = (name or "").lower()
    for term in terms:
        canon = recognize_type(term)
        if canon and term.lower() in low:
            return canon
    return ""   # multi-material, none named in the title -> ambiguous -> hold


def _pick_color(color_raw: str) -> str:
    """One canonical colour: the first term that names a real colour ('Gold / Yellow' -> Gold), skipping the
    non-colour noise ('Multi-Color', 'Wooden'). '' -> the variety's Natural colour floor downstream."""
    for term in _terms(color_raw):
        canon = extract_color(term)
        if canon:
            return canon
    return ""


def _pick_finish(finish_raw: str) -> str:
    """One surface finish, dropping 'Bookmatched' (a slab layout, not a finish). Canonicalised when known;
    an unknown finish passes through raw and takes the pack's flagged last-resort finish downstream."""
    for term in _terms(finish_raw):
        if term.lower() == "bookmatched":
            continue
        return _finish_lookup().get(match_key(term), "") or term
    return ""


def _pick_thickness(thickness_raw: str) -> str:
    """The first real 'N mm' thickness (fuleistone lists several offered thicknesses)."""
    for term in _terms(thickness_raw):
        if any(ch.isdigit() for ch in term):
            return term
    return ""


def _variety(name: str) -> str:
    """The variety candidate for the tree matcher. A live-inventory lot wraps the variety as
    'Instock Slabs - <variety> [- Bookmatched] - <area>m2'; unwrap it. Otherwise strip only the format word
    (colour+type tokens kept), so a real variety resolves and a generic descriptor routes to review."""
    import re
    text = (name or "").strip()
    lot = re.match(r"(?i)^instock\s+slabs\s*[-–]\s*(.+?)\s*[-–]\s*(?:bookmatched\s*[-–]\s*)?[\d.]+\s*m²", text)
    if lot:
        return strip_format(lot.group(1).strip())
    return strip_format(text)


class FuleistoneAdapter(AdapterBase):
    source = "fuleistone"
    adapter_version = "1.0.0"
    variety_match_key = "name"       # the variety is extracted from the name (see _variety)
    format_field = "format"          # the scraper tags every row 'slab' (constant category)
    generic_descriptor = True        # name is a colour+type descriptor; only deterministic matches auto-accept
    required_columns = ["product_id", "name", "material", "image_urls"]
    # SKU is always blank; product_id (WooCommerce) is the stable key. Keep raw_name required (not the natural
    # key) so a rare id-less product still mints a surrogate downstream instead of dropping here.
    required_canonical = ("raw_name",)

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("product_id")),
        "src_url": lambda r: AdapterBase.clean(r.get("permalink")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": lambda r: AdapterBase.clean(r.get("name")),
        "variety_match_key": lambda r: _variety(AdapterBase.clean(r.get("name"))),
        "raw_type": lambda r: _pick_type(r.get("material"), r.get("name")),
        "raw_color": lambda r: _pick_color(r.get("color")),
        "raw_finish": lambda r: _pick_finish(r.get("finish")),
        # every row is a slab (constant category emitted by the scraper); the Format Resolver honours the tag
        "raw_format": lambda r: AdapterBase.clean(r.get("format")),
        "raw_thickness": lambda r: _pick_thickness(r.get("thickness")),
        # faces parsed from the Size attribute (mm); blank when Size is qualitative -> derive fills the pack
        # default. build_dims is the ONE shared helper; unit declared here since the scraper emits bare numbers.
        "raw_dimensions": lambda r: AdapterBase.build_dims(
            r.get("dimensions_length"), r.get("dimensions_height"), unit="mm", blank_na=True),
        # live-inventory lots carry a REAL piece count (Quantity '…, 83pcs'); derive prefers it over the area
        # estimate. The general catalogue has neither -> the in-stock flag seeds the per-category fallback.
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("slab_count")),
        # total available area (m2) from the product name -> derive divides it into a count when no count exists
        "raw_stock_m2": lambda r: AdapterBase.clean(r.get("stock_m2")),
        # structured availability ('in-stock') -> derive's stock fallback when no area/count is present
        "raw_stock_status": lambda r: AdapterBase.clean(r.get("stock_status")),
        "raw_description": lambda r: AdapterBase.clean(r.get("description")) or AdapterBase.clean(r.get("short_description")),
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls") or r.get("image_urls_thumb"), "|"),
    }


ADAPTER = FuleistoneAdapter()
