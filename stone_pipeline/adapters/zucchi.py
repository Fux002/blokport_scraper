"""Zucchi adapter (section 7 Stage 1, section 14 M11).

Clean named-variety source: product_name_en is the variety (Acadian Night).
`family` is the stone type; finish and classification map cleanly. Zucchi has no
colour column, but the structured Portuguese name carries it
(CIN {type} {colour} {trade}, e.g. "Granito Preto Rio Negro"), with the English
name and description as secondary sources; a light scan recovers it as a raw_
input for Stage 3. Slab bundles with real metric dimensions, weights, slab counts.
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
        "raw_color": lambda r: _color(r),   # no colour column; recovered from the name (see _color)
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        "raw_quality": lambda r: AdapterBase.clean(r.get("classification")),
        "raw_format": lambda r: AdapterBase.clean(r.get("format")),
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        "raw_dimensions": lambda r: _dims(r),   # lambda defers to _dims defined below
        "raw_weight": lambda r: _per_piece_kg(r),   # per-BUNDLE net -> canonical PER-PIECE (see below)
        "raw_total_m2": lambda r: AdapterBase.clean(r.get("area_m2")),
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("slab_count")),
        "raw_description": lambda r: AdapterBase.clean(r.get("description")),
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls"), "|"),
    }


# Colour lives in the structured Portuguese name (CIN {type} {colour} {trade}); the English name and the
# description are secondary sources for rows where the Portuguese name omits it. First non-empty wins, so
# the authoritative PT colour beats a conflicting English trade name ("Wonderful Black" vs PT "Cinza").
_COLOUR_SOURCE_FIELDS = ("product_name_pt", "product_name_en", "description")


def _color(r: dict) -> str:
    for field in _COLOUR_SOURCE_FIELDS:
        colour = extract_color(AdapterBase.clean(r.get(field)))
        if colour:
            return colour
    return ""


def _dims(r: dict) -> str:
    length = AdapterBase.clean(r.get("length_m"))
    height = AdapterBase.clean(r.get("height_m"))
    if not length and not height:
        return ""
    return f"length={length}m;height={height}m"


def _per_piece_kg(r: dict) -> str:
    """Zucchi reports weight_kg_net for the whole BUNDLE; normalize to the canonical PER-PIECE weight by
    dividing by slab_count. This is the ONLY place that knows zucchi's per-bundle format, so the shared
    pipeline stays agnostic (source-isolation invariant). Blank when slab_count is missing/0 -> derive
    synthesizes a per-piece value. The health gate bounds weight_kg_net + slab_count, so a source drift
    that would break this per-bundle assumption aborts before ingest rather than silently mis-scaling."""
    net = AdapterBase.clean(r.get("weight_kg_net"))
    count = AdapterBase.clean(r.get("slab_count"))
    try:
        n, c = float(net), float(count)
    except (TypeError, ValueError):
        return ""
    return str(round(n / c, 3)) if (n > 0 and c > 0) else ""


ADAPTER = ZucchiAdapter()
