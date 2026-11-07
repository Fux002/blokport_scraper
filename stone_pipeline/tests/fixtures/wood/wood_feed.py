"""A NON-website source for the wood smoke run (test fixture, copied into the run's tmp workspace only).

It has no scrape file: `load_frame` hands the pipeline its records directly, the way an API, database or partner
feed would (the adapters/_api_template.py shape). The records are the workspace's wood_feed.json, standing in for
the feed's response."""

from __future__ import annotations

import json

import polars as pl

from stone_pipeline.adapters.base import AdapterBase
from stone_pipeline.config.settings import SETTINGS

FEED = "wood_feed.json"


class WoodFeedAdapter(AdapterBase):
    source = "woodfeed"
    adapter_version = "1.0.0"
    variety_match_key = "name"
    format_field = "form"
    required_columns = ("product_id", "name", "species", "form", "pieces", "image_url")
    required_canonical = ("src_natural_key", "raw_name")

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("product_id")),
        "src_url": lambda r: AdapterBase.clean(r.get("url")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("timestamp")),
        "raw_name": lambda r: AdapterBase.clean(r.get("name")),
        "variety_match_key": lambda r: AdapterBase.clean(r.get("name")),
        "raw_type": lambda r: AdapterBase.clean(r.get("species")),
        "raw_color": lambda r: AdapterBase.clean(r.get("color")),
        "raw_finish": lambda r: AdapterBase.clean(r.get("finish")),
        "raw_quality": lambda r: AdapterBase.clean(r.get("quality")),
        "raw_format": lambda r: AdapterBase.clean(r.get("form")),
        "raw_thickness": lambda r: AdapterBase.clean(r.get("thickness")),
        "raw_dimensions": lambda r: AdapterBase.build_dims(r.get("length"), r.get("width"), unit="m"),
        "raw_slab_count": lambda r: AdapterBase.clean(r.get("pieces")),      # the schema's piece-count field
        "raw_image_urls": lambda r: [AdapterBase.clean(r.get("image_url"))],
        "raw_description": lambda r: "",
    }

    def load_frame(self, scrape_path=None):
        records = json.loads((SETTINGS.paths.workspace_root / FEED).read_text(encoding="utf-8"))
        if not records:
            return None
        return pl.DataFrame(records), records[0]["timestamp"], f"{self.source}://feed"


ADAPTER = WoodFeedAdapter()
