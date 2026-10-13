"""Sentas Marble adapter.

Named-variety source: the scraper takes the variety from the site's own stock index (/en/stocks), so the
variety column is clean and needs no name surgery; the bundle caption ("B1103 - 2CM CALACATTA GREEN
POLISHED") is kept only as raw capture. Type comes from the site's collection grids where it lists the
variety, else is left for the matched variety to supply. Colour is the page's own "Colors" field, else a
colour word in the variety name (as varsha does). The site publishes no quality grade, so quality is left
blank and the shared last-resort rule grades and flags it. Dimensions arrive in cm with their unit;
weight is the bundle total and is normalised to the canonical per-piece value here.

Stock: where the bundle has a packing list it is the stock source (its IN STOCK, reserved included) and its
sold record; a packing list that failed to load or could not be read holds the stock (never the site's
possibly different figure in its place); without one the site's piece count is the stock.

Lots: Sentas is BUNDLE-shaped. The bundle's per-slab scans map to raw_pieces and its printed reference
("B6392" in "B6392 - 2 CM ...") to src_lot_label; the shared loader (stages/lots.py) decides whether the
bundle becomes its pieces. The lot reference is the bundle's own site id, never the printed reference,
which the vendor does not keep unique ("B898-899", "T7").
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.adapters.tokens import extract_color


def _variety(record) -> str:
    return AdapterBase.clean(record.get("variety"))


def _printed_ref(record) -> str:
    """The vendor's printed bundle reference, the caption's lead before ' - ' ('B6392'); '' when none."""
    lead, separator, _ = AdapterBase.clean(record.get("caption")).partition(" - ")
    return lead.strip() if separator else ""


def _stock(record) -> str:
    """The bundle's slabs in stock: the packing list's figure, '' (held) when it could not be read, or the
    site's piece count when the bundle has no packing list."""
    status = AdapterBase.clean(record.get("packing_status"))
    if status == "ok":
        return AdapterBase.clean(record.get("packing_available"))
    if status:
        return ""
    return AdapterBase.clean(record.get("pieces"))


def _sold(record) -> str:
    """The packing list's sold count; '' when the bundle has no readable packing list (no sale record)."""
    if AdapterBase.clean(record.get("packing_status")) != "ok":
        return ""
    return AdapterBase.clean(record.get("packing_sold"))


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
    adapter_version = "1.3.0"
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
        "raw_color": lambda r: AdapterBase.clean(r.get("color")) or extract_color(_variety(r)),
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        # the site publishes no quality grade: blank on purpose, so the pack's last-resort grade applies,
        # flagged for review
        "raw_quality": lambda r: "",
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        # 'width' is the long face on sentas -> length; 'height' the short face -> the raw dims 'height' key.
        # Values carry their unit from the page ("255 cm"), so unit="" here.
        "raw_dimensions": lambda r: AdapterBase.build_dims(r.get("width_cm"), r.get("height_cm"), unit=""),
        "raw_weight": _per_piece_kg,
        "raw_total_m2": lambda r: AdapterBase.clean(r.get("total_sqm")),
        "raw_slab_count": _stock,
        "raw_sold_count": _sold,
        "raw_description": lambda r: "",   # the site has no product description
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls"), "|"),
        # lots: the per-slab scans as written by the scraper (JSON, passed through untouched) + the label
        "raw_pieces": lambda r: (r.get("piece_scans") or "").strip(),
        "src_lot_label": _printed_ref,
    }


ADAPTER = SentasAdapter()
