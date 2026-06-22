"""Zucchi adapter (section 7 Stage 1, section 14 M11).

Clean named-variety source: product_name_en is the variety (Acadian Night).
`family` is the stone type; finish and classification map cleanly. Zucchi has no
colour column, but the rich description and the name often name a colour
("black granite"); a light scan recovers it as a raw_ input for Stage 3. Slab
bundles with real metric dimensions, weights, and slab counts.
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.adapters.tokens import extract_color


class ZucchiAdapter(AdapterBase):
    source = "zucchi"
    adapter_version = "1.1.0"
    variety_match_key = "product_name_en"
    format_field = "format"  # scraper should emit the explicit block/slab/tile tag here
    required_columns = ["bundle_id", "product_name_en", "family", "finish",
                        "classification", "thickness", "image_urls"]
    required_canonical = ("src_natural_key", "raw_name")

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("bundle_id")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": lambda r: AdapterBase.clean(r.get("product_name_en")),
        "variety_match_key": lambda r: AdapterBase.clean(r.get("product_name_en")),
        "raw_type": lambda r: AdapterBase.clean(r.get("family")),
        # colour is not a column; recover it from the name then the description
        "raw_color": lambda r: extract_color(AdapterBase.clean(r.get("product_name_en")))
        or extract_color(AdapterBase.clean(r.get("description"))),
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        "raw_quality": lambda r: AdapterBase.clean(r.get("classification")),
        "raw_format": lambda r: AdapterBase.clean(r.get("format")),
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        "raw_dimensions": lambda r: _dims(r),
        "raw_weight": lambda r: AdapterBase.clean(r.get("weight_kg_net")),
        "raw_total_m2": lambda r: AdapterBase.clean(r.get("area_m2")),
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("slab_count")),
        "raw_description": lambda r: AdapterBase.clean(r.get("description")),
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls"), "|"),
    }


def _dims(r: dict) -> str:
    length = AdapterBase.clean(r.get("length_m"))
    height = AdapterBase.clean(r.get("height_m"))
    if not length and not height:
        return ""
    return f"length={length}m;height={height}m"


ADAPTER = ZucchiAdapter()
