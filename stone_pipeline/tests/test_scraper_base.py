"""ScraperBase framework: per-source/per-run output layout, source-namespaced
image names (no cross-site mixup), a validated format column, and a thin scraper.
Network is stubbed so the test runs offline.
"""

from __future__ import annotations

import csv

import pytest

from scrapers.base import ScraperBase


class _Fake(ScraperBase):
    source = "demosite"
    category = None  # per-product format
    columns = ["product_id", "name"]
    download_images_enabled = True  # exercise the download/naming path

    def list_products(self):
        return [
            {"id": 32429, "name": "Cream Marble Tile", "fmt": "tile", "imgs": ["http://x/a.jpg", "http://x/b.png"]},
            {"id": 620, "name": "Alpine Slab", "fmt": "slab", "imgs": ["http://x/c.jpg"]},
            {"id": 99, "name": "No Format", "fmt": "", "imgs": []},
        ]

    def parse_product(self, raw):
        return {"product_id": raw["id"], "name": raw["name"],
                "image_urls": raw["imgs"], "format": raw["fmt"]}

    def download_images(self, product_id, urls):  # stub the network
        out = []
        for i, u in enumerate(urls, 1):
            fn = f"{self.source}_{self._safe(product_id)}_{i}{self._ext(u)}"
            (self.images_dir / fn).write_bytes(b"img")
            out.append(fn)
        return out


@pytest.fixture
def run(tmp_path):
    s = _Fake(data_dir=tmp_path)
    csv_path = s.run()
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    return s, csv_path, rows


def test_per_source_per_run_layout(run, tmp_path):
    s, csv_path, _ = run
    assert csv_path.parent.parent.name == "demosite"  # data/<source>/<ts>/
    files = {p.relative_to(csv_path.parent).as_posix() for p in csv_path.parent.rglob("*") if p.is_file()}
    assert "products.csv" in files and "scrape.log" in files


def test_images_are_source_namespaced(run):
    s, csv_path, rows = run
    imgs = {p.name for p in s.images_dir.iterdir()}
    # source prefix makes them unique even if two sites reuse product id 32429
    assert "demosite_32429_1.jpg" in imgs and "demosite_32429_2.png" in imgs
    assert "demosite_620_1.jpg" in imgs


def test_format_column_present_and_validated(run):
    s, csv_path, rows = run
    by_id = {r["product_id"]: r for r in rows}
    assert by_id["32429"]["format"] == "tile"
    assert by_id["620"]["format"] == "slab"
    # an unknown format is recorded as a failure (not silently accepted)
    assert any(f["kind"] == "format" for f in s._failures)


def test_csv_has_site_and_base_columns(run):
    _, _, rows = run
    cols = set(rows[0].keys())
    assert {"product_id", "name"} <= cols  # site columns
    assert {"format", "image_count", "image_urls", "image_filenames_local",
            "scrape_timestamp", "raw_json"} <= cols  # base columns


def test_raw_json_captures_full_source(run):
    # nothing available is lost: the full source object is preserved
    import json
    _, _, rows = run
    raw = json.loads(rows[0]["raw_json"])
    assert raw["id"] == 32429 and raw["fmt"] == "tile"


def test_default_uses_source_links_no_download(tmp_path):
    # default (download disabled): the upload uses source URLs, no local images
    class _Linked(_Fake):
        download_images_enabled = False

    s = _Linked(data_dir=tmp_path)
    s.run()
    rows = list(csv.DictReader(open(s.products_csv, encoding="utf-8")))
    by_id = {r["product_id"]: r for r in rows}
    assert by_id["32429"]["image_urls"] == "http://x/a.jpg | http://x/b.png"  # source links
    assert by_id["32429"]["image_filenames_local"] == ""  # nothing downloaded
    assert not s.images_dir.exists()  # no images folder created
