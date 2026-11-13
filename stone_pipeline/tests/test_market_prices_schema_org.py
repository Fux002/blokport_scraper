"""The generic retail collector (schema_org_product), on fixtures only: a site's register entry points (sitemaps,
listing pages), its product pages' schema.org Product/Offer structured data, and the honest reasons a site gives no
rows. Nothing here can reach a real site: the scripted transport fails on any URL it was not given."""

from __future__ import annotations

import gzip
import json
import time
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from market_prices import config
from market_prices.collect import collect_run
from market_prices.fetching import Response, TransportError
from market_prices.register import Register

MOMENT = datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)
BASE = "https://retail.example"
ROBOTS = f"{BASE}/robots.txt"
ROBOTS_OPEN = Response(200, "User-agent: *\nDisallow:\n")
SITEMAP = f"{BASE}/sitemap-products.xml"
PRODUCT_PATH = r"^/p/[a-z0-9-]+/?$"


def _site(site_id="retail", base_url=BASE, discovery=None, **overrides) -> dict:
    site = {
        "site_id": site_id, "seller_name": "Retail", "seller_type": "merchant", "seller_country": "GB",
        "served_market": "GB", "platform": "schema_org_product", "base_url": base_url,
        "discovery": discovery if discovery is not None else {"sitemaps": ["/sitemap-products.xml"],
                                                              "product_path": PRODUCT_PATH},
        "terms": {"terms_url": f"{base_url}/terms", "checked_on": "2026-11-13", "summary": "No clause on copying."},
        "decision": "approved", "decided_by": "owner", "decided_on": "2026-11-13",
    }
    return {**site, **overrides}


def _register(*sites: dict) -> Register:
    return Register.model_validate({"brand": "wudport", "sites": list(sites or [_site()])})


class _Sites:
    """A scripted transport. Any URL it was not told about fails the test: nothing is fetched by accident."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.requested: list[str] = []

    def get(self, url: str) -> Response:
        self.requested.append(url)
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _no_pause(_seconds: float) -> None:
    pass


def _run(answers: dict, register=None):
    return collect_run(register or _register(), _Sites(answers), MOMENT, _no_pause)


def _status(result, site_id="retail"):
    return next(status for status in result.statuses if status.site_id == site_id)


def _urlset(*paths: str, base=BASE) -> Response:
    locs = "".join(f"<url><loc>{base}{path}</loc></url>" for path in paths)
    return Response(200, f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                         f"{locs}</urlset>", {"Content-Type": "application/xml"})


def _index(*urls: str) -> Response:
    locs = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    return Response(200, f'<?xml version="1.0"?><sitemapindex>{locs}</sitemapindex>', {"Content-Type": "application/xml"})


def _product(sku="PLY-18", name="Hardwood plywood 18mm 2440 x 1220", price="38.50", **offer) -> dict:
    return {"@context": "https://schema.org", "@type": "Product", "name": name, "sku": sku,
            "brand": {"@type": "Brand", "name": "Timberco"}, "gtin13": "5012345678900",
            "width": {"@type": "QuantitativeValue", "value": "1220", "unitCode": "MMT"},
            "additionalProperty": [{"@type": "PropertyValue", "name": "Thickness", "value": "18", "unitText": "mm"}],
            "offers": {"@type": "Offer", "price": price, "priceCurrency": "GBP",
                       "availability": "https://schema.org/InStock", **offer}}


def _page(*blocks, raw: str = "") -> Response:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(block)}</script>' for block in blocks)
    return Response(200, f"<html><head>{scripts}{raw}</head><body>Product</body></html>",
                    {"Content-Type": "text/html; charset=utf-8"})


def _shop(pages: dict, sitemap=None, robots=ROBOTS_OPEN) -> dict:
    """A site whose sitemap lists `pages` (path -> page response)."""
    return {ROBOTS: robots, SITEMAP: sitemap or _urlset(*pages), **{f"{BASE}{path}": page for path, page in pages.items()}}


# --- the register ----------------------------------------------------------------------------------------------------
def test_a_retail_site_loads_with_its_entry_points():
    site = _register(_site(discovery={"sitemaps": ["/sitemap.xml"], "listing_pages": ["/sheet/plywood"],
                                      "product_path": PRODUCT_PATH})).sites[0]
    assert (site.discovery.sitemaps, site.discovery.listing_pages) == (("/sitemap.xml",), ("/sheet/plywood",))


@pytest.mark.parametrize("change", [
    {"discovery": {}},                                                          # no entry point, no product_path
    {"discovery": {"product_path": PRODUCT_PATH}},                              # no entry point
    {"discovery": {"sitemaps": ["/sitemap.xml"]}},                              # no product_path
    {"discovery": {"sitemaps": ["/sitemap.xml"], "product_path": "(["}},         # not a regex
    {"discovery": {"sitemaps": ["sitemap.xml"], "product_path": PRODUCT_PATH}},  # not a path
    {"discovery": {"sitemaps": ["//evil.example/s.xml"], "product_path": PRODUCT_PATH}},  # another host
    {"discovery": {"sitemaps": ["https://retail.example/s.xml"], "product_path": PRODUCT_PATH}},  # a URL
    {"discovery": {"listing_pages": ["/a b"], "product_path": PRODUCT_PATH}},   # whitespace
    {"discovery": {"listing_pages": ["/a#b"], "product_path": PRODUCT_PATH}},   # a fragment
    {"query": {"page": "1"}},                                                   # this platform takes no query
])
def test_a_bad_retail_entry_is_refused(change):
    with pytest.raises(ValidationError):
        _register(_site(**change))


def test_a_retail_site_without_entry_points_is_refused():
    site = _site()
    del site["discovery"]
    with pytest.raises(ValidationError, match="discovery"):
        _register(site)


def test_a_woocommerce_site_may_not_carry_entry_points():
    site = _site(platform="woocommerce_store")
    with pytest.raises(ValidationError, match="discovery"):
        _register(site)


# --- discovery -------------------------------------------------------------------------------------------------------
def test_product_pages_come_from_the_sitemap_in_sorted_order_and_only_matching_on_site_paths():
    pages = {"/p/b-board": _page(_product("B")), "/p/a-board": _page(_product("A"))}
    sitemap = _urlset("/p/b-board", "/p/a-board", "/about-us", "/p/A-UPPER/extra")
    sitemap = Response(200, sitemap.text.replace("</urlset>", "<url><loc>https://other.example/p/x</loc></url></urlset>"))
    answers = _shop(pages, sitemap=sitemap)
    sites = _Sites(answers)
    result = collect_run(_register(), sites, MOMENT, _no_pause)
    assert sites.requested == [ROBOTS, SITEMAP, f"{BASE}/p/a-board", f"{BASE}/p/b-board"]
    assert [row.listing_key for row in result.observations] == ["A", "B"]
    assert _status(result).status.value == "ok"


def test_a_sitemap_index_is_followed():
    child = f"{BASE}/sitemap-products-1.xml"
    answers = {ROBOTS: ROBOTS_OPEN, SITEMAP: _index(child), child: _urlset("/p/a"), f"{BASE}/p/a": _page(_product("A"))}
    assert [row.listing_key for row in _run(answers).observations] == ["A"]


def test_an_index_deeper_than_the_cap_stops_the_site_with_the_reason():
    urls = [f"{BASE}/s{depth}.xml" for depth in range(config.SCHEMA_ORG_MAX_SITEMAP_DEPTH + 1)]
    answers = {ROBOTS: ROBOTS_OPEN, SITEMAP: _index(urls[0])}
    answers.update({url: _index(nxt) for url, nxt in zip(urls, urls[1:])})
    answers[urls[-1]] = _index(f"{BASE}/never.xml")
    status = _status(_run(answers))
    assert status.status.value == "unavailable_error"
    assert "sitemap index nested deeper" in status.detail


def test_more_sitemaps_than_the_cap_stop_the_site():
    children = [f"{BASE}/s{n}.xml" for n in range(config.SCHEMA_ORG_MAX_SITEMAPS + 1)]
    answers = {ROBOTS: ROBOTS_OPEN, SITEMAP: _index(*children), **{url: _urlset() for url in children}}
    status = _status(_run(answers))
    assert status.status.value == "unavailable_error" and "sitemaps" in status.detail


def test_an_off_site_sitemap_in_an_index_is_never_requested():
    answers = {ROBOTS: ROBOTS_OPEN, SITEMAP: _index("https://other.example/s.xml"), }
    status = _status(_run(answers))
    assert status.status.value == "unavailable_error"


def test_a_gzipped_sitemap_is_read():
    body = gzip.compress(_urlset("/p/a").text.encode())
    answers = {ROBOTS: ROBOTS_OPEN, SITEMAP: Response(200, body.decode("latin-1"), {}, body),
               f"{BASE}/p/a": _page(_product("A"))}
    assert [row.listing_key for row in _run(answers).observations] == ["A"]


def test_a_gzipped_sitemap_over_the_unzipped_cap_is_refused():
    body = gzip.compress(b"<urlset>" + b" " * (config.SCHEMA_ORG_MAX_UNZIPPED_BYTES + 1) + b"</urlset>")
    answers = {ROBOTS: ROBOTS_OPEN, SITEMAP: Response(200, "", {}, body)}
    status = _status(_run(answers))
    assert status.status.value == "unavailable_error" and "unzipped" in status.detail


def _listing_page(*hrefs: str, next_href: str = "") -> Response:
    links = "".join(f'<a class="tile" href="{href}">Item</a>' for href in hrefs)
    following = f'<link rel="next" href="{next_href}">' if next_href else ""
    return Response(200, f"<html><head>{following}</head><body>{links}</body></html>", {"Content-Type": "text/html"})


def test_listing_pages_give_product_links_and_their_next_pages():
    register = _register(_site(discovery={"listing_pages": ["/sheet/plywood"], "product_path": PRODUCT_PATH}))
    answers = {
        ROBOTS: ROBOTS_OPEN,
        f"{BASE}/sheet/plywood": _listing_page("/p/a", "p/ignored-relative", "https://other.example/p/z",
                                               "/p/b#reviews", next_href="/sheet/plywood?page=2"),
        f"{BASE}/sheet/plywood?page=2": _listing_page("/p/c", f"{BASE}/p/a", next_href="/sheet/plywood"),
        f"{BASE}/p/a": _page(_product("A")), f"{BASE}/p/b": _page(_product("B")), f"{BASE}/p/c": _page(_product("C")),
    }
    sites = _Sites(answers)
    result = collect_run(register, sites, MOMENT, _no_pause)
    assert [row.listing_key for row in result.observations] == ["A", "B", "C"]
    assert sites.requested.count(f"{BASE}/sheet/plywood") == 1      # a next link back to a read page is not re-read
    assert _status(result).status.value == "ok"


def test_listing_pages_past_the_cap_stop_the_site():
    register = _register(_site(discovery={"listing_pages": ["/l0"], "product_path": PRODUCT_PATH}))
    answers = {ROBOTS: ROBOTS_OPEN}
    for n in range(config.SCHEMA_ORG_MAX_LISTING_PAGES + 1):
        answers[f"{BASE}/l{n}"] = _listing_page(next_href=f"/l{n + 1}")
    status = _status(_run(answers, register))
    assert status.status.value == "unavailable_error" and "listing pages" in status.detail


def test_product_pages_over_the_cap_read_the_first_ones_and_say_so(monkeypatch):
    monkeypatch.setattr(config, "SCHEMA_ORG_MAX_PRODUCT_PAGES", 2)
    pages = {f"/p/{key}": _page(_product(key.upper())) for key in ("a", "b", "c")}
    result = _run(_shop(pages))
    assert [row.listing_key for row in result.observations] == ["A", "B"]
    status = _status(result)
    assert status.status.value == "partial" and "3 product pages" in status.detail


def test_a_product_page_robots_forbids_is_skipped_unrequested_and_counted():
    robots = Response(200, "User-agent: *\nDisallow: /p/secret\n")
    pages = {"/p/open": _page(_product("OPEN")), "/p/secret": _page(_product("SECRET"))}
    sites = _Sites(_shop(pages, robots=robots))
    result = collect_run(_register(), sites, MOMENT, _no_pause)
    assert f"{BASE}/p/secret" not in sites.requested
    status = _status(result)
    assert (status.status.value, status.malformed_listings) == ("partial", 1)
    assert "robots.txt disallows the product page x1" in status.detail


def test_robots_forbidding_an_entry_point_stops_the_site():
    robots = Response(200, "User-agent: *\nDisallow: /sitemap\n")
    assert _status(_run({ROBOTS: robots})).status.value == "disallowed_robots"


def test_a_dead_product_page_is_that_listings_problem_alone():
    pages = {"/p/a": _page(_product("A")), "/p/gone": Response(404, "<html>Not found</html>")}
    result = _run(_shop(pages))
    assert [row.listing_key for row in result.observations] == ["A"]
    status = _status(result)
    assert status.status.value == "partial" and "product page HTTP 404 x1" in status.detail


def test_a_block_on_a_product_page_ends_the_site_at_once():
    pages = {"/p/a": Response(429, "slow down"), "/p/b": _page(_product("B"))}
    sites = _Sites(_shop(pages))
    status = _status(collect_run(_register(), sites, MOMENT, _no_pause))
    assert status.status.value == "unavailable_blocked" and status.rows == 0
    assert f"{BASE}/p/b" not in sites.requested


def test_a_timeout_on_a_product_page_stops_the_site_and_keeps_what_was_read():
    pages = {"/p/a": _page(_product("A")), "/p/b": TransportError("timed out"), "/p/c": _page(_product("C"))}
    result = _run(_shop(pages))
    assert [row.listing_key for row in result.observations] == ["A"]
    assert _status(result).status.value == "partial"


# --- the structured data ---------------------------------------------------------------------------------------------
def test_a_product_and_its_offer_give_an_exact_price_and_the_stated_facts():
    row = _run(_shop({"/p/ply": _page(_product())})).observations[0]
    assert (row.listing_key, row.listing_text, row.source_url) == ("PLY-18", "Hardwood plywood 18mm 2440 x 1220",
                                                                   f"{BASE}/p/ply")
    assert (row.price_amount, row.currency, row.price_status.value, row.price_text) == ("38.50", "GBP", "priced",
                                                                                         "38.50 GBP")
    assert row.availability_text == "InStock"
    assert row.stated_specs == {"brand": "Timberco", "gtin13": "5012345678900", "sku": "PLY-18", "width": "1220 MMT",
                                "Thickness": "18 mm"}
    assert row.collector_version == "schema_org_product-1"
    assert (row.provenance["price_amount"].value, row.provenance["currency"].value) == ("listing", "listing")


def test_a_number_price_keeps_its_exact_digits():
    block = json.dumps(_product()).replace('"38.50"', "38.50")
    page = Response(200, f'<script type="application/ld+json">{block}</script>', {"Content-Type": "text/html"})
    assert _run(_shop({"/p/a": page})).observations[0].price_amount == "38.50"


@pytest.mark.parametrize("price", ["38,50", "0", "0.00", "", "1e3", "-5", "about 40", None])
def test_a_price_that_is_not_one_exact_positive_decimal_is_no_price_and_its_text_is_kept(price):
    row = _run(_shop({"/p/a": _page(_product(price=price))})).observations[0]
    assert (row.price_status.value, row.price_amount) == ("none", None)
    if price not in ("", None):
        assert row.price_text == f"{price} GBP"


def test_a_range_is_no_price_and_equal_bounds_are_one_price():
    ranged = _product(); ranged["offers"] = {"@type": "AggregateOffer", "lowPrice": "20", "highPrice": "45",
                                             "priceCurrency": "GBP"}
    single = _product("S"); single["offers"] = {"@type": "AggregateOffer", "lowPrice": "30.00", "highPrice": "30.00",
                                                "priceCurrency": "GBP"}
    rows = {row.listing_key: row for row in _run(_shop({"/p/r": _page(ranged), "/p/s": _page(single)})).observations}
    assert (rows["PLY-18"].price_status.value, rows["PLY-18"].price_text) == ("none", "20-45 GBP")
    assert (rows["S"].price_status.value, rows["S"].price_amount) == ("priced", "30.00")


def test_several_offers_are_one_price_only_when_they_agree():
    agree = _product("A"); agree["offers"] = [{"@type": "Offer", "price": "10", "priceCurrency": "GBP"},
                                              {"@type": "Offer", "price": "10", "priceCurrency": "GBP"}]
    differ = _product("D"); differ["offers"] = [{"@type": "Offer", "price": "10", "priceCurrency": "GBP"},
                                                {"@type": "Offer", "price": "12", "priceCurrency": "GBP"}]
    rows = {row.listing_key: row for row in _run(_shop({"/p/a": _page(agree), "/p/d": _page(differ)})).observations}
    assert (rows["A"].price_status.value, rows["A"].price_amount) == ("priced", "10")
    assert (rows["D"].price_status.value, rows["D"].price_text) == ("none", "10-12 GBP")


def test_a_price_specification_is_read_when_the_offer_has_no_price():
    product = _product(price=None)
    product["offers"]["priceSpecification"] = {"@type": "UnitPriceSpecification", "price": "41.20",
                                               "priceCurrency": "GBP", "valueAddedTaxIncluded": False}
    row = _run(_shop({"/p/a": _page(product)})).observations[0]
    assert (row.price_amount, row.currency, row.vat_basis.value) == ("41.20", "GBP", "excl")


def test_the_graph_type_lists_and_nested_entities_are_walked():
    product = _product()
    product["@type"] = ["Product", "Thing"]
    block = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage", "mainEntity": product},
                                                          {"@type": "Organization", "name": "Retail"}]}
    assert _run(_shop({"/p/a": _page(block)})).observations[0].listing_key == "PLY-18"


def test_a_product_group_gives_one_listing_per_variant():
    group = {"@context": "https://schema.org", "@type": "ProductGroup", "name": "OSB3 board",
             "hasVariant": [_product("OSB-11", "OSB3 11mm", "12.00"), _product("OSB-18", "OSB3 18mm", "19.00")]}
    rows = _run(_shop({"/p/osb": _page(group)})).observations
    assert [(row.listing_key, row.price_amount) for row in rows] == [("OSB-11", "12.00"), ("OSB-18", "19.00")]


def test_the_pages_own_product_is_chosen_among_related_ones():
    own = _product("OWN"); own["url"] = f"{BASE}/p/a"
    related = _product("REL"); related["url"] = f"{BASE}/p/other"
    rows = _run(_shop({"/p/a": _page(related, own)})).observations
    assert [row.listing_key for row in rows] == ["OWN"]


def test_several_products_with_none_the_pages_own_are_ambiguous_not_guessed():
    status = _status(_run(_shop({"/p/a": _page(_product("X"), _product("Y"))})))
    assert status.status.value == "unavailable_error"
    assert "several schema.org Products and none is the page's own x1" in status.detail


def test_a_page_with_no_structured_product_says_so():
    status = _status(_run(_shop({"/p/a": Response(200, "<html><body>£38.50</body></html>",
                                                  {"Content-Type": "text/html"})})))
    assert (status.status.value, status.rows) == ("unavailable_error", 0)
    assert "no schema.org Product on the page x1" in status.detail


def test_a_broken_block_is_skipped_when_another_holds_the_product():
    row = _run(_shop({"/p/a": _page(_product(), raw='<script type="application/ld+json">{broken</script>')})).observations[0]
    assert row.listing_key == "PLY-18"


def test_without_a_sku_the_page_path_is_the_key():
    product = _product(); del product["sku"]
    assert _run(_shop({"/p/a": _page(product)})).observations[0].listing_key == "/p/a"


def test_a_product_without_a_name_is_malformed():
    product = _product(); del product["name"]
    assert "Product has no name x1" in _status(_run(_shop({"/p/a": _page(product)}))).detail


def test_a_product_url_off_the_site_falls_back_to_the_page():
    product = _product(); product["url"] = "https://other.example/p/a"
    assert _run(_shop({"/p/a": _page(product)})).observations[0].source_url == f"{BASE}/p/a"


def test_unclosed_scripts_are_read_in_bounded_time():
    page = Response(200, '<script type="application/ld+json">' * 20000, {"Content-Type": "text/html"})
    started = time.monotonic()
    status = _status(_run(_shop({"/p/a": page})))
    assert time.monotonic() - started < 5
    assert status.status.value == "unavailable_error"


def test_hostile_nesting_is_that_blocks_problem_alone():
    deep = '<script type="application/ld+json">' + "[" * 100000 + "]" * 100000 + "</script>"
    row = _run(_shop({"/p/a": _page(_product(), raw=deep)})).observations[0]
    assert row.listing_key == "PLY-18"


# --- VAT, as stated --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("stated, expected", [(True, "incl"), ("true", "incl"), (False, "excl"), ("False", "excl")])
def test_the_offers_own_vat_statement_is_the_listings(stated, expected):
    row = _run(_shop({"/p/a": _page(_product(valueAddedTaxIncluded=stated))})).observations[0]
    assert (row.vat_basis.value, row.provenance["vat_basis"].value) == (expected, "listing")


def test_without_the_offers_statement_the_sites_applies():
    register = _register(_site(statements={"vat_basis": "excl"}))
    row = _run(_shop({"/p/a": _page(_product())}), register).observations[0]
    assert (row.vat_basis.value, row.provenance["vat_basis"].value) == ("excl", "site_statement")


def test_a_listing_vat_differing_from_the_sites_wins_and_is_counted():
    register = _register(_site(statements={"vat_basis": "excl"}))
    pages = {"/p/a": _page(_product("A", valueAddedTaxIncluded=True)), "/p/b": _page(_product("B"))}
    result = _run(_shop(pages), register)
    rows = {row.listing_key: row for row in result.observations}
    assert (rows["A"].vat_basis.value, rows["B"].vat_basis.value) == ("incl", "excl")
    status = _status(result)
    assert status.status.value == "ok" and status.malformed_listings == 0
    assert status.detail == "listing VAT differs from the site statement x1"


# --- the reasons -----------------------------------------------------------------------------------------------------
def test_the_detail_names_the_most_common_malformed_reasons():
    pages = {f"/p/n{n}": _page() for n in range(3)}
    pages.update({"/p/gone": Response(410, "gone"), "/p/ok": _page(_product("OK"))})
    detail = _status(_run(_shop(pages))).detail
    assert detail == ("4 malformed listings skipped: no schema.org Product on the page x3; product page HTTP 410 x1")


def test_reasons_past_the_cap_are_summed_not_dropped(monkeypatch):
    monkeypatch.setattr(config, "MALFORMED_REASONS_SHOWN", 1)
    pages = {"/p/n1": _page(), "/p/n2": _page(), "/p/gone": Response(404, "gone"), "/p/ok": _page(_product("OK"))}
    detail = _status(_run(_shop(pages))).detail
    assert detail == "3 malformed listings skipped: no schema.org Product on the page x2; other reasons x1"
