"""Polonine adapter (section 7 Stage 1, section 14 M3).

Polonine is a clean named-variety source: the `material` column carries the
variety name (ALPINE, ARABESCATTO EXTRA), so variety_match_key is `material`
and rows resolve cleanly downstream. It is a slabware export, all slab bundles,
so format is Slab. stone_type carries a trailing render tag ("Granite /") that
is stripped here. Dimensions arrive as metres in dimension_length/height with
the thickness as the slab width.
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase


class PolonineAdapter(AdapterBase):
    source = "polonine"
    adapter_version = "1.1.0"
    variety_match_key = "material"
    # the scrape column that should carry the explicit block/slab/tile tag
    format_field = "format"
    required_columns = [
        "product_id",
        "material",
        "stone_type",
        "color",
        "finish",
        "quality",
        "thickness",
        "slab_count",
        "area_sqmt",
        "image_urls",
    ]
    required_canonical = ("src_natural_key", "raw_name")

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("product_id")),
        "src_url": lambda r: AdapterBase.clean(r.get("detail_url")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": lambda r: AdapterBase.clean(r.get("material")),
        "variety_match_key": lambda r: AdapterBase.clean(r.get("material")),
        # stone_type carries a trailing "/" tag on polonine
        "raw_type": lambda r: AdapterBase.strip_trailing_token(r.get("stone_type"), "/"),
        "raw_color": lambda r: AdapterBase.clean(r.get("color")),
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        "raw_quality": lambda r: AdapterBase.clean(r.get("quality")),
        # the scraper should emit an explicit format tag in this column; until it
        # does the value is empty and the Format Resolver infers it structurally
        "raw_format": lambda r: AdapterBase.clean(r.get("format")),
        "raw_origin": lambda r: AdapterBase.clean(r.get("origin")),
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        # length + height both metres on polonine; width comes from thickness (kept in raw_thickness)
        "raw_dimensions": lambda r: AdapterBase.build_dims(
            r.get("dimension_length"), r.get("dimension_height"), unit="m"),
        "raw_weight": lambda r: AdapterBase.clean(r.get("weight")),
        "raw_total_m2": lambda r: AdapterBase.clean(r.get("area_sqmt")),
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("slab_count")),
        "raw_slabs_array": lambda r: AdapterBase.clean(r.get("slabs_detail")),
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls"), "|"),
        # polonine has no description column
        "raw_description": lambda r: "",
    }


ADAPTER = PolonineAdapter()
