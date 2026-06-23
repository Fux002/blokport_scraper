"""Marenostone adapter (section 7 Stage 1, section 14 M11).

Generic-descriptor source: the `name` is "Cream Marble Tile" with no named
variety, so variety_match_key is the name with colour/type/format tokens stripped
(usually empty), and such rows legitimately resolve to a null variation and route
to the tree-gap queue. This is correct behaviour, not a failure. Colour lives in
attr_category2, type in attr_category1, format in attr_format (Tile routes to
Slabs). Some SKUs are blank and mint a surrogate downstream.
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.adapters.tokens import strip_variety


class MarenostoneAdapter(AdapterBase):
    source = "marenostone"
    adapter_version = "1.2.0"
    variety_match_key = "name"  # extracted, not a clean column
    format_field = "attr_format"  # marenostone already carries an explicit format tag
    # generic-descriptor source: the variety candidate is the name minus the
    # format word (colour+type kept). The matcher auto-accepts only deterministic
    # (exact/projection) hits for such sources and routes fuzzy/phonetic to review,
    # so a real variety (White Travertine) resolves but a generic descriptor
    # (Cream Marble) is not silently bound to one specific stone.
    generic_descriptor = True
    required_columns = ["product_id", "name", "attr_category1", "attr_category2",
                        "attr_format", "attr_finish", "image_urls"]
    # do NOT require src_natural_key: blank SKUs are expected and mint downstream
    # in Stage 2 (section 2), they must not be dropped here
    required_canonical = ("raw_name",)

    field_map = {
        # sku is the declared natural key; it ships blank on some rows and mints
        "src_natural_key": lambda r: AdapterBase.clean(r.get("sku")),
        "src_url": lambda r: AdapterBase.clean(r.get("permalink")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": lambda r: AdapterBase.clean(r.get("name")),
        # primary candidate variety: descriptor minus colour/type/format tokens
        # (empty for a purely generic descriptor). When empty, the variation stage
        # falls back to a strict canonical-name match on the colour+type name, so a
        # real variety named by colour+type (White Travertine) still resolves
        "variety_match_key": lambda r: strip_variety(AdapterBase.clean(r.get("name"))),
        "raw_type": lambda r: AdapterBase.clean(r.get("attr_category1")),
        "raw_color": lambda r: AdapterBase.clean(r.get("attr_category2")),
        "raw_finish": lambda r: AdapterBase.clean(r.get("attr_finish")),
        "raw_quality": lambda r: AdapterBase.clean(r.get("attr_quality")),
        # explicit format tag from the source; the Format Resolver decides, with
        # the product name and structure as fallbacks (no blind default here)
        "raw_format": lambda r: AdapterBase.clean(r.get("attr_format")),
        # the scraper puts the depth (Thickness) in dimensions_width -> our thickness/width
        "raw_thickness": lambda r: _na(r.get("dimensions_width")),
        "raw_dimensions": lambda r: _dims(r),
        "raw_weight": lambda r: _na(r.get("weight")),
        "raw_description": lambda r: AdapterBase.clean(r.get("description")) or AdapterBase.clean(r.get("short_description")),
        # base scrapers now emit `image_urls`; fall back to the legacy column name
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls") or r.get("image_urls_full"), "|"),
    }


def _na(value) -> str:
    text = AdapterBase.clean(value)
    return "" if text.upper() == "N/A" else text


def _dims(r: dict) -> str:
    length = _na(r.get("dimensions_length"))
    height = _na(r.get("dimensions_height"))
    if not length and not height:
        return ""
    return f"length={length};height={height}"


ADAPTER = MarenostoneAdapter()
