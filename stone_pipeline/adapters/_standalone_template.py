"""TEMPLATE for a STANDALONE-category adapter (e.g. accessories) — copy, rename,
fill in. A standalone category does not share the stone-variety vocabulary, so the
adapter's job is just to map the scraped product columns to the canonical row and
tag the category via raw_format = the category name. Matching is then done by that
category's matcher (stages/match_variation.STANDALONE_MATCHERS), not the stone
-variety engine. See CATEGORY_GUIDE.md (section B).

This file is not registered and not imported by the pipeline; it is a starting
point only.
"""

from __future__ import annotations

from stone_pipeline.adapters.base import AdapterBase


class StandaloneAdapterTemplate(AdapterBase):
    source = "REPLACE_ME"             # the scrape source id, e.g. "acme_accessories"
    adapter_version = "0.1.0"
    # the category this source feeds; must match a CATEGORIES entry name and a
    # ScraperBase `category` (e.g. "accessory"). Held until that category is active.
    category_name = "accessory"

    # variety_match_key is the column carrying the product's identifying name/SKU
    # the matcher will key on (a standalone category matches by name/SKU, not by the
    # stone-variety vocabulary).
    variety_match_key = "name"
    format_field = "format"

    # the scrape columns Stage 0 must see (set to the real accessory columns)
    required_columns = ["product_id", "name", "image_urls"]
    required_canonical = ("src_natural_key", "raw_name")

    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("product_id")),
        "src_url": lambda r: AdapterBase.clean(r.get("detail_url")),
        "scrape_timestamp": lambda r: AdapterBase.clean(r.get("scrape_timestamp")),
        "raw_name": lambda r: AdapterBase.clean(r.get("name")),
        "variety_match_key": lambda r: AdapterBase.clean(r.get("name")),
        # TAG THE CATEGORY: this is what routes the row to the standalone path.
        "raw_format": lambda r: "accessory",
        # add the rest of the accessory-specific raw_ fields here as needed
        # (sku, brand, material, dimensions, price, ...). They flow through as raw_
        # inputs that the accessory matcher / derive stage interpret.
        "raw_image_urls": lambda r: AdapterBase.split_list(r.get("image_urls"), "|"),
    }


# ADAPTER = StandaloneAdapterTemplate()   # register in the adapter REGISTRY when ready
