"""ScraperBase framework: per-source/per-run output layout, source-namespaced
image names (no cross-site mixup), a validated format column, and a thin scraper.
Network is stubbed so the test runs offline.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone

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


# --- HTTP preventive throttle + 429 handling (WS1) ----------------------------
class _StubResp:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.content = b"ok"

    def raise_for_status(self):
        pass


class _SeqClient:
    """Returns the given status codes in order, recording each request's User-Agent."""
    def __init__(self, statuses, headers=None):
        self.statuses = list(statuses)
        self.headers = headers or {}
        self.i = 0
        self.uas: list[str] = []

    def request(self, method, url, headers=None, **kw):
        self.uas.append((headers or {}).get("User-Agent"))
        status = self.statuses[min(self.i, len(self.statuses) - 1)]
        self.i += 1
        return _StubResp(status, self.headers)


def _sleepless(monkeypatch):
    """Collect sleep durations and allow the fake test hosts past the SSRF guard."""
    sleeps: list[float] = []
    monkeypatch.setattr("scrapers.base.time.sleep", lambda t: sleeps.append(t))
    monkeypatch.setattr("scrapers.base.url_allowed", lambda u: True)
    return sleeps


def test_request_delay_spaces_each_request(tmp_path, monkeypatch):
    class _Throttled(_Fake):
        request_delay = (0.5, 0.5)   # fixed so the value is assertable
    s = _Throttled(data_dir=tmp_path)
    s._client = _SeqClient([200])
    sleeps = _sleepless(monkeypatch)
    s.get("http://x/a")
    assert sleeps == [0.5]            # exactly one preventive delay per logical request


def test_request_delay_default_off_no_sleep(tmp_path, monkeypatch):
    class _Off(_Fake):
        request_delay = (0.0, 0.0)
    s = _Off(data_dir=tmp_path)
    s._client = _SeqClient([200])
    sleeps = _sleepless(monkeypatch)
    s.get("http://x/a")
    assert sleeps == []              # disabled -> no preventive delay


def test_image_path_is_not_throttled(tmp_path, monkeypatch):
    class _Throttled(_Fake):
        request_delay = (0.5, 0.5)
    s = _Throttled(data_dir=tmp_path)
    s._client = _SeqClient([200])
    sleeps = _sleepless(monkeypatch)
    s.get("http://x/img.jpg", throttle=False)   # how download_images calls it
    assert sleeps == []


def test_preventive_delay_paid_once_then_429_backoff_separate(tmp_path, monkeypatch):
    class _Throttled(_Fake):
        request_delay = (0.5, 0.5)
    s = _Throttled(data_dir=tmp_path)
    s._client = _SeqClient([429, 200], headers={"Retry-After": "1"})
    sleeps = _sleepless(monkeypatch)
    s.get("http://x/a")
    # preventive delay is paid ONCE at entry (not per retry); the 429 Retry-After wait is on top
    assert sleeps == [0.5, 1.0]


def test_retry_after_both_forms_and_clamp(tmp_path):
    from datetime import timedelta
    from email.utils import format_datetime
    from scrapers.base import _RETRY_AFTER_MAX
    s = _Fake(data_dir=tmp_path)
    assert s._retry_after_seconds("30", 0) == 30.0                 # delta-seconds
    assert s._retry_after_seconds("99999", 0) == _RETRY_AFTER_MAX  # clamped
    assert s._retry_after_seconds(None, 0) == s.backoff_base ** 0 * 5   # absent -> backoff (5)
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=45))
    assert 30 < s._retry_after_seconds(future, 0) <= _RETRY_AFTER_MAX   # HTTP-date -> ~45s
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=45))
    assert s._retry_after_seconds(past, 2) == s.backoff_base ** 2 * 5   # past date -> backoff (20)


def test_user_agent_stable_across_a_run(tmp_path, monkeypatch):
    from scrapers.base import _USER_AGENTS
    _sleepless(monkeypatch)
    s = _Fake(data_dir=tmp_path)
    cap = _SeqClient([200, 200])
    s._client = cap
    s._request("GET", "http://x/1", throttle=False)
    s._request("GET", "http://x/2", throttle=False)
    assert s._ua in _USER_AGENTS
    assert cap.uas == [s._ua, s._ua]   # one stable UA per session, not per request


# --- ergonomics: single-source registry + stable failure schema (WS4) ---------
def test_registry_single_source_of_truth_auto_discovers_classes():
    from scrapers.run import REGISTRY, _SOURCES
    # names are declared ONCE (_SOURCES); each class is discovered from its module, no import list to sync
    assert list(REGISTRY) == list(_SOURCES)
    assert all(issubclass(c, ScraperBase) and c is not ScraperBase for c in REGISTRY.values())
    assert REGISTRY["marenostone"].__name__ == "MarenoStoneScraper"


def test_record_failure_has_a_stable_triage_schema(tmp_path):
    s = _Fake(data_dir=tmp_path)
    s.record_failure("dims", bundle_id="B7", url="http://x/p", error=RuntimeError("boom"))
    f = s._failures[-1]
    assert f["kind"] == "dims" and f["ref"] == "B7"      # ref derived from the id-like kwarg
    assert f["url"] == "http://x/p" and f["error"] == "boom"   # error stringified
    s.record_failure("parse", error="bad")               # no id -> ref blank, still keyed by kind
    assert s._failures[-1]["ref"] == "" and s._failures[-1]["kind"] == "parse"


# --- fetch-failed signal: carry + audit + producer (WS2) ----------------------
def test_mark_fetch_failed_carries_and_audits(tmp_path):
    s = _Fake(data_dir=tmp_path)
    row: dict = {}
    s.mark_fetch_failed(row, "dims", url="http://x/p", error="boom")
    s.mark_fetch_failed(row, "dims")     # same group again -> deduped, not re-audited
    s.mark_fetch_failed(row, "weight")
    assert row[ScraperBase.FETCH_FAILED_COL] == "dims|weight"   # carried, deduped pipe-list
    assert [f["kind"] for f in s._failures] == ["dims", "weight"]   # audited once per (row, group)


def test_fetch_failed_column_is_a_base_column(tmp_path):
    # the reserved column MUST be in BASE_COLUMNS or products.csv's extrasaction="ignore" would drop it
    assert ScraperBase.FETCH_FAILED_COL in ScraperBase.BASE_COLUMNS


def _boom(*a, **k):
    raise RuntimeError("rate limited (HTTP 429)")


def test_marenostone_page_dims_signals_failure_vs_absence(tmp_path, monkeypatch):
    from scrapers.marenostone import MarenoStoneScraper
    s = MarenoStoneScraper(data_dir=tmp_path)
    assert s._page_dims("") == ({}, True)             # no page == genuine absence, NOT a failure
    monkeypatch.setattr(s, "get", _boom)
    assert s._page_dims("http://marenostone/p") == ({}, False)          # fetch failed
    assert "http://marenostone/p" not in s.__dict__.get("_dims_cache", {})   # failure not cached -> retryable


def test_marenostone_parse_product_marks_dims_fetch_failure(tmp_path, monkeypatch):
    from scrapers.marenostone import MarenoStoneScraper
    s = MarenoStoneScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "get", _boom)
    row = s.parse_product({"id": 1, "name": "X Marble Slab",
                           "permalink": "http://marenostone/x", "attributes": [], "images": []})
    assert row[ScraperBase.FETCH_FAILED_COL] == "dims"     # the row carries the hold signal
    assert any(f["kind"] == "dims" for f in s._failures)   # and it is audited


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


# -- SlabWare slab/block classifier (varsha and any block-selling SlabWare tenant) ------------------

def test_slabware_classify_format_reads_thickness_not_the_name():
    from scrapers import slabware
    # MULTI thickness is the block sentinel (a solid block has no single slab gauge); case/space insensitive.
    assert slabware.classify_format("MULTI") == "block"
    assert slabware.classify_format(" multi ") == "block"
    # every real gauge is a slab (including thick slabs), regardless of any "Z"/"ZB" naming.
    for t in ("2cm", "3cm", "5cm", "8CM", "14 CM"):
        assert slabware.classify_format(t) == "slab", t
    # missing thickness falls to the default kind -- never a fabricated block.
    assert slabware.classify_format("") == "slab"
    assert slabware.classify_format(None) == "slab"


def test_slabware_classifier_output_is_a_valid_scrape_format():
    from scrapers import slabware
    from scrapers.base import VALID_FORMATS
    assert {slabware.classify_format("MULTI"), slabware.classify_format("2cm")} <= set(VALID_FORMATS)
