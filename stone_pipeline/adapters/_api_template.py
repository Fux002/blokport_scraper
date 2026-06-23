"""TEMPLATE for a NON-FILE data source (API / DB / partner feed) — copy, rename, fill in.

A normal scraper writes data/<source>/<ts>/products.csv and the adapter just maps it. For a source
that has NO scrape file — a REST API, a database, a partner's live export — you additionally
override `load_frame`: fetch the records, build a polars frame in the SAME column shape your
`field_map` expects, and return (frame, timestamp_token, origin_label). Everything downstream
(adapt -> stages -> emit -> catalog) is identical; the rest of the pipeline never knows or cares
where the rows came from.

    ADAPTER is auto-discovered (no registry to edit). Add a sources.yaml entry + a fixture, same
    as any adapter. Run it with `python -m stone_pipeline.run <source>`.
"""

from __future__ import annotations

import polars as pl

from stone_pipeline.adapters.base import AdapterBase


class ApiSourceAdapter(AdapterBase):
    source = "myapi"                       # rename
    adapter_version = "1.0.0"
    variety_match_key = "name"             # the column carrying the stone's name
    required_columns = ("product_id", "name")
    format_field = "format"               # column carrying slab/block/tile, if the source tags it

    # map the source's columns -> the canonical schema (same as a CSV adapter)
    field_map = {
        "src_natural_key": lambda r: AdapterBase.clean(r.get("product_id")),
        "raw_name": lambda r: AdapterBase.clean(r.get("name")),
        "raw_type": lambda r: AdapterBase.clean(r.get("stone_type")),
        # ... colour / finish / dimensions / image url, as the source provides
    }

    def load_frame(self, scrape_path=None):
        """Fetch the records however this source provides them and return them as a frame.
        This is the ONLY extra method a non-file source needs."""
        records = self._fetch()                       # <- your API call / DB query / feed read
        if not records:
            return None                               # nothing to ingest this run
        frame = pl.DataFrame(records)
        timestamp_token = "20260101_000000"           # <- a real stamp (e.g. from the API response)
        return frame, timestamp_token, f"{self.source}://live"

    def _fetch(self) -> list[dict]:
        # e.g. requests.get(...).json()["products"], or a DB cursor -> list of dicts whose keys
        # match what field_map reads. Returns [] when there is nothing new.
        raise NotImplementedError("fill in the API/DB fetch")


# ADAPTER = ApiSourceAdapter()   # uncomment in the real file to auto-register
