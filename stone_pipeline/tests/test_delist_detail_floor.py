"""A1: the delist gate must not trust a scrape whose per-product DETAIL fetches were badly degraded.
An identity-from-detail scraper DROPS a product whose detail fetch failed -- indistinguishable from a real
absence -- so a rate-limited run would silently discontinue held products. The scraper records the detail
failure ratio in its completion marker (ScraperBase.note_detail); run._scrape_detail_failure_ratio reads it
and the delist gate suppresses delisting above the floor. Keyed on the fetch-failure ratio, never the
delisted count, so a genuine catalog shrink still delists.
"""

from __future__ import annotations

import json

from scrapers.base import ScraperBase
from stone_pipeline import run as run_mod


class _DetailScraper(ScraperBase):
    source = "detailsite"
    category = "slab"
    columns = ["product_id"]

    def __init__(self, outcomes, **kw):
        super().__init__(**kw)
        self._outcomes = outcomes

    def list_products(self):
        for i, ok in enumerate(self._outcomes):
            self.note_detail(ok=ok)          # simulate one detail fetch per product
            if ok:
                yield {"id": i}

    def parse_product(self, raw):
        return {"product_id": raw["id"], "image_urls": []}


def _ratio_in_marker(scraper) -> float | None:
    marker = json.loads(scraper.complete_marker.read_text(encoding="utf-8"))
    return marker.get("detail_failure_ratio")


def test_marker_records_the_detail_failure_ratio(tmp_path):
    s = _DetailScraper([True, True, False, True], data_dir=tmp_path)   # 1 of 4 failed
    s.run()
    assert _ratio_in_marker(s) == 0.25


def test_marker_omits_the_ratio_when_no_detail_fetch_reported(tmp_path):
    # a scraper that never calls note_detail must not write a ratio -> the gate reads 0.0 (no-op).
    class _NoDetail(ScraperBase):
        source = "nodetail"
        category = "slab"
        columns = ["product_id"]

        def list_products(self):
            return [{"id": 1}]

        def parse_product(self, raw):
            return {"product_id": raw["id"], "image_urls": []}

    s = _NoDetail(data_dir=tmp_path)
    s.run()
    assert _ratio_in_marker(s) is None


def test_run_reads_the_ratio_from_the_scrape_marker(tmp_path):
    s = _DetailScraper([True, False, False, False], data_dir=tmp_path)   # 3 of 4 failed = 0.75
    products_csv = s.run()
    assert run_mod._scrape_detail_failure_ratio(products_csv) == 0.75


def test_run_ratio_is_zero_when_marker_absent_or_malformed(tmp_path):
    assert run_mod._scrape_detail_failure_ratio(None) == 0.0
    # a products.csv whose sibling marker is missing -> 0.0 (never raises)
    (tmp_path / "products.csv").write_text("product_id\n1\n", encoding="utf-8")
    assert run_mod._scrape_detail_failure_ratio(tmp_path / "products.csv") == 0.0
    # a malformed marker -> 0.0
    (tmp_path / "scrape_complete.json").write_text("{not json", encoding="utf-8")
    assert run_mod._scrape_detail_failure_ratio(tmp_path / "products.csv") == 0.0


def test_ratio_crosses_the_configured_floor(tmp_path):
    from stone_pipeline.config.settings import SETTINGS
    floor = SETTINGS.thresholds.delist_detail_failure_floor
    low = _DetailScraper([True] * 19 + [False], data_dir=tmp_path)     # 5% < floor
    low.run()
    assert run_mod._scrape_detail_failure_ratio(low.products_csv) < floor
    high = _DetailScraper([True, False, False], data_dir=tmp_path)     # 66% >= floor
    high.run()
    assert run_mod._scrape_detail_failure_ratio(high.products_csv) >= floor
