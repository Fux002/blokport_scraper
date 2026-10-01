"""Sentas Marble adapter.

Named-variety source: the scraper takes the variety from the site's own stock index (/en/stocks), so the
variety column is clean and needs no name surgery; the bundle caption ("B1103 - 2CM CALACATTA GREEN
POLISHED") is kept only as raw capture. Type comes from the site's collection grids where it lists the
variety, else is left for the matched variety to supply. The site publishes no colour and no quality
grade: colour is recovered from a colour word in the variety name (as varsha/zucchi do) and quality is
left blank so the shared last-resort rule grades and flags it. Dimensions arrive in cm with their unit;
weight is the bundle total and is normalised to the canonical per-piece value here.
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.adapters.tokens import extract_color


def _variety(record) -> str:
    return AdapterBase.clean(record.get("variety"))


def _per_piece_kg(record) -> str:
    """Sentas renders the bundle's total kg; canonical weight is PER PIECE, so divide by the piece count.
    Blank when either figure is missing or non-positive -> derive synthesises a per-piece value."""
    try:
        total, pieces = float(AdapterBase.clean(record.get("total_kg"))), float(AdapterBase.clean(record.get("pieces")))
    except (TypeError, ValueError):
        return ""
    return str(round(total / pieces, 3)) if (total > 0 and pieces > 0) else ""


class SentasAdapter(AdapterBase):
    source = "sentas"
    adapter_version = "1.0.0"
    variety_match_key = "variety"
    format_field = "format"
    required_columns = ["product_id", "variety", "url", "finish", "thickness",
                        "height_cm", "width_cm", "pieces", "image_urls"]
    required_canonical = ("src_natural_key", "raw_name")

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("product_id")),
        "src_url": lambda r: AdapterBase.clean(r.get("url")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": _variety,
        "variety_match_key": _variety,
        "raw_type": lambda r: AdapterBase.clean(r.get("stone_type")),
        "raw_color": lambda r: extract_color(_variety(r)),
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        # the site publishes no quality grade (its "( BH-12 )" code is an unlabelled vendor code, not a
        # grade): blank on purpose, so the pack's last-resort grade applies, flagged for review
        "raw_quality": lambda r: "",
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        # 'width' is the long face on sentas -> length; 'height' the short face -> the raw dims 'height' key.
        # Values carry their unit from the page ("255 cm"), so unit="" here.
        "raw_dimensions": lambda r: AdapterBase.build_dims(r.get("width_cm"), r.get("height_cm"), unit=""),
        "raw_weight": _per_piece_kg,
        "raw_total_m2": lambda r: AdapterBase.clean(r.get("total_sqm")),
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("pieces")),
        "raw_description": lambda r: "",   # the site has no product description
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls"), "|"),
    }


ADAPTER = SentasAdapter()
