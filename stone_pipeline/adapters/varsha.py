"""Varsha adapter (section 7 Stage 1, section 14 M11).

Named-variety slabware source: material_name is the variety (Alaska Gold). Composition is
a classification tag like "EXOTIC /", not a stone type, so type is left for the matched
variety to supply; colour comes from the `color` column when present, else is recovered
from a colour word in the variety name. Dimensions come from the per-slab width/height pipe
lists; take the first slab. Many varieties are exotic granites absent from the slabs
reference and legitimately gap.
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.adapters.tokens import extract_color

# varsha prefixes many material names with an internal code that is NOT part of the official
# variety ('Z ASTORIA', 'Z B FUSION BLACK', collapsed 'ZB PATAGONIA'). No regex is hardcoded
# for it: AdapterBase auto-discovers the 'z'/'zb' code prefixes from the batch (they fan out
# across many varieties) and strips them, like it would any new scraper's codes.


def _variety(record) -> str:
    return AdapterBase.clean(record.get("material_name"))  # base strips discovered codes


class VarshaAdapter(AdapterBase):
    source = "varsha"
    adapter_version = "1.3.0"
    variety_match_key = "material_name"
    format_field = "format"  # scraper should emit the explicit block/slab/tile tag here
    required_columns = ["bundle_id", "material_name", "finish", "quality",
                        "thickness", "slab_count", "photo_urls"]
    required_canonical = ("src_natural_key", "raw_name")

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("bundle_id")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": _variety,
        "variety_match_key": _variety,
        # composition is a classification tag, not a stone type; leave type to the
        # matched variety (Stage 5 is authoritative)
        "raw_type": lambda r: "",
        "raw_color": lambda r: AdapterBase.clean(r.get("color")) or extract_color(_variety(r)),
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        "raw_quality": lambda r: AdapterBase.clean(r.get("quality")),
        "raw_format": lambda r: AdapterBase.clean(r.get("format")),
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        "raw_dimensions": lambda r: _dims(r),   # lambda defers to _dims defined below
        "raw_total_m2": lambda r: AdapterBase.clean(r.get("total_sqmt")),
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("slab_count")),
        "raw_origin": lambda r: AdapterBase.clean(r.get("country_code")),
        "raw_description": lambda r: AdapterBase.clean(r.get("description")),
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("photo_urls"), "|"),
    }


def _dims(r: dict) -> str:
    width = AdapterBase.first_of(r.get("slab_widths_m"), "|")
    height = AdapterBase.first_of(r.get("slab_heights_m"), "|")
    if not width and not height:
        return ""
    # width is the long edge on these exports; map it to length, height to height
    return f"length={width}m;height={height}m"


ADAPTER = VarshaAdapter()
