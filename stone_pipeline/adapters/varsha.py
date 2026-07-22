"""Varsha adapter (section 7 Stage 1, section 14 M11).

Named-variety slabware source: material_name is the variety (Alaska Gold). Composition is a MIXED
field -- its head is a real stone type for most rows ("Granite / ", "Quartzite / ") and a
classification tag for the rest ("EXOTIC / ", "Stock Clearance") -- so type is taken from it only when
it names a known Medusa type, else left for the matched variety to supply. Colour comes from the
`color` column when present, else is recovered from a colour word in the variety name. Dimensions come
from the per-slab width/height pipe lists; take the first slab. Many varieties are exotic granites
absent from the slabs reference and legitimately gap.
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.adapters.tokens import extract_color, recognize_type

# varsha prefixes many material names with an internal code that is NOT part of the official
# variety ('Z ASTORIA', 'Z B FUSION BLACK', collapsed 'ZB PATAGONIA'). No regex is hardcoded
# for it: AdapterBase auto-discovers the 'z'/'zb' code prefixes from the batch (they fan out
# across many varieties) and strips them, like it would any new scraper's codes.


def _variety(record) -> str:
    return AdapterBase.clean(record.get("material_name"))  # base strips discovered codes


def _composition_type(record) -> str:
    """composition is a MIXED field: its head is a real stone type for ~60% of rows ('Granite / ',
    'Quartzite / ') and a classification tag for the rest ('Exotic / ', 'Stock Clearance'). Read it as the
    stone type ONLY when the head names a known Medusa type; leave empty for a tag, so a genuinely
    type-less variety is held for review rather than typed from a tag. The '/' separator is varsha's own
    format, so the split stays here (source isolation); type recognition uses the shared live vocab."""
    return recognize_type(AdapterBase.clean((record.get("composition") or "").split("/")[0]))


class VarshaAdapter(AdapterBase):
    source = "varsha"
    adapter_version = "1.4.0"
    variety_match_key = "material_name"
    format_field = "format"  # scraper should emit the explicit block/slab/tile tag here
    required_columns = ["bundle_id", "material_name", "finish", "quality",
                        "thickness", "slab_count", "photo_urls"]
    required_canonical = ("src_natural_key", "raw_name")

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("bundle_id")),
        "src_url": lambda r: AdapterBase.clean(r.get("detail_url")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": _variety,
        "variety_match_key": _variety,
        # composition is a real stone type for most rows and a classification tag for the rest;
        # take it as the type only when it names a known Medusa type (else leave it to Stage 5)
        "raw_type": _composition_type,
        "raw_color": lambda r: AdapterBase.clean(r.get("color")) or extract_color(_variety(r)),
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        "raw_quality": lambda r: AdapterBase.clean(r.get("quality")),
        "raw_format": lambda r: AdapterBase.clean(r.get("format")),
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        # width is the long edge on these exports -> map it to length (first arg), height to height
        "raw_dimensions": lambda r: AdapterBase.build_dims(
            AdapterBase.first_of(r.get("slab_widths_m"), "|"),
            AdapterBase.first_of(r.get("slab_heights_m"), "|"), unit="m"),
        "raw_total_m2": lambda r: AdapterBase.clean(r.get("total_sqmt")),
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("slab_count")),
        "raw_origin": lambda r: AdapterBase.clean(r.get("country_code")),
        "raw_description": lambda r: AdapterBase.clean(r.get("description")),
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("photo_urls"), "|"),
    }


ADAPTER = VarshaAdapter()
