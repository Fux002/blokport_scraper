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


def test_marenostone_parses_dimensions_from_attributes_table():
    # the real Length/Width/Thickness live in the product page's WooCommerce attributes table,
    # not the Store API; the scraper must read them (the unit comes from the label).
    from scrapers.marenostone import _dims_from_html
    html = ('<table><tr><th class="woocommerce-product-attributes-item__label">Length (cm)</th>'
            '<td class="woocommerce-product-attributes-item__value"><p>140</p></td></tr>'
            '<tr><th class="woocommerce-product-attributes-item__label">Width (cm)</th>'
            '<td class="woocommerce-product-attributes-item__value"><p>35</p></td></tr>'
            '<tr><th class="woocommerce-product-attributes-item__label">Thickness (cm)</th>'
            '<td class="woocommerce-product-attributes-item__value"><p>3</p></td></tr></table>')
    assert _dims_from_html(html) == {"length": "140cm", "width": "35cm", "thickness": "3cm"}
    assert _dims_from_html("<table></table>") == {}  # no dims -> empty, never crashes


def test_marenostone_declared_unit_and_free_length():
    from scrapers.marenostone import MarenoStoneScraper, _dims_from_html
    L = 'woocommerce-product-attributes-item__label'
    V = 'woocommerce-product-attributes-item__value'
    # source declares its unit -> a label with NO (unit) uses the declared 'cm', not a hardcoded guess
    assert MarenoStoneScraper.dimension_unit == "cm"
    no_unit = f'<table><tr><th class="{L}">Length</th><td class="{V}"><p>200</p></td></tr></table>'
    assert _dims_from_html(no_unit, default_unit="cm") == {"length": "200cm"}
    # a non-numeric 'Free' length keeps its raw text (no fabricated 'Freecm') -> rejects downstream as no size
    free = f'<table><tr><th class="{L}">Length (cm)</th><td class="{V}"><p>Free</p></td></tr></table>'
    assert _dims_from_html(free) == {"length": "Free"}


def test_curl_cffi_uses_proxy_only_when_needs_proxy(tmp_path, monkeypatch):
    # the residential proxy is metered; a curl_cffi site applies it ONLY when needs_proxy is set (its
    # Cloudflare tenant blocks the datacenter IP). A curl_cffi site that works direct spends no proxy.
    import sys
    import types

    captured: dict = {}
    fake_req = types.ModuleType("curl_cffi.requests")
    fake_req.Session = lambda **opts: captured.update(opts) or object()
    fake_cffi = types.ModuleType("curl_cffi")
    fake_cffi.requests = fake_req
    monkeypatch.setitem(sys.modules, "curl_cffi", fake_cffi)
    monkeypatch.setitem(sys.modules, "curl_cffi.requests", fake_req)
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy.soax.com:1337")

    class _Proxied(_Fake):
        use_curl_cffi = True
        needs_proxy = True

    class _Direct(_Fake):
        use_curl_cffi = True
        needs_proxy = False

    captured.clear()
    _Proxied(data_dir=tmp_path)._cffi()
    assert "proxies" in captured                     # needs_proxy -> residential proxy applied

    captured.clear()
    _Direct(data_dir=tmp_path)._cffi()
    assert "proxies" not in captured                 # curl_cffi alone -> no proxy spent


# --- the proxy toolbox: capability-based resolution (Phase 5) ------------------
def test_proxy_capability_resolves_via_the_toolbox(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:1337")

    class _Cap(_Fake):
        proxy_capability = "cloudflare_residential"

    assert _Cap(data_dir=tmp_path)._resolve_proxy() == "http://u:p@proxy:1337"


def test_capability_without_secret_connects_direct(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOKPORT_SCRAPER_PROXY", raising=False)

    class _Cap(_Fake):
        proxy_capability = "cloudflare_residential"

    assert _Cap(data_dir=tmp_path)._resolve_proxy() is None   # loud warn, connects direct


def test_legacy_needs_proxy_still_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://legacy:1")

    class _Legacy(_Fake):
        needs_proxy = True                            # no capability -> the legacy single-proxy fallback

    assert _Legacy(data_dir=tmp_path)._resolve_proxy() == "http://legacy:1"


def test_no_capability_no_needs_proxy_is_direct(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://x:1")
    assert _Fake(data_dir=tmp_path)._resolve_proxy() is None   # default: metered proxy never spent
