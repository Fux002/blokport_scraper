"""The competitor-price path, on fixtures only: the row, the terms gate, block handling, the WooCommerce collector and
the delivery. Nothing here, or in the package, can reach a real site."""

from __future__ import annotations

import ast
import csv
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from botocore.exceptions import ClientError

import market_prices
from market_prices import config
from market_prices.collect import collect_run, stamp_for, write_run
from market_prices.fetching import Response, TransportError
from market_prices.publish import publish_run
from market_prices.register import RegisterError, Register, Site, load_register
from market_prices.robots import parse_robots
from stone_pipeline.io.deliverables import file_entry

MOMENT = datetime(2026, 11, 8, 12, 30, 5, tzinfo=timezone.utc)
ROBOTS_OPEN = Response(200, "User-agent: *\nDisallow:\n")
BASE = "https://shop.example"
ROBOTS = f"{BASE}/robots.txt"


def _site(site_id="shop", base_url=BASE, **overrides) -> dict:
    site = {
        "brand": "blokport", "site_id": site_id, "seller_name": "Shop", "seller_type": "merchant",
        "seller_country": "BE", "served_market": "NL", "platform": "woocommerce_store", "base_url": base_url,
        "terms": {"terms_url": f"{base_url}/terms", "checked_on": "2026-11-02", "summary": "No clause on copying."},
        "decision": "approved", "decided_by": "owner", "decided_on": "2026-11-08",
    }
    return {**site, **overrides}


def _register(*sites: dict) -> Register:
    return Register.model_validate({"brand": "blokport", "sites": list(sites or [_site()])})


def _listing(listing_id=1, **overrides) -> dict:
    listing = {
        "id": listing_id, "name": f"Sill &amp; step {listing_id}", "permalink": f"{BASE}/product/{listing_id}/",
        "sku": f"SKU-{listing_id}", "short_description": "<p>Honed <b>edge</b></p>",
        "price_html": "<span>&euro;&nbsp;21,25</span>",
        "prices": {"price": "2125", "currency_code": "EUR", "currency_minor_unit": 2},
        "attributes": [{"name": "Materiaal", "terms": [{"name": "Hardsteen"}]}],
        "dimensions": {"length": "90", "width": "15", "height": "2"}, "weight": "20",
        "stock_availability": {"text": "Ruim op voorraad"}, "add_to_cart": {"minimum": 1},
    }
    return {**listing, **overrides}


def _page(items, total=1, **headers) -> Response:
    return Response(200, json.dumps(items), {"X-WP-TotalPages": str(total), "Content-Type": "application/json", **headers})


def _page_url(page: int, base_url=BASE) -> str:
    return f"{base_url}{config.WOO_STORE_PATH}?page={page}&per_page={config.WOO_PAGE_SIZE}"


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


def _shop(*pages, robots=ROBOTS_OPEN) -> _Sites:
    return _Sites({ROBOTS: robots, **{_page_url(n): page for n, page in enumerate(pages, 1)}})


def _no_pause(_seconds: float) -> None:
    pass


def _run(sites, register=None):
    return collect_run(register or _register(), sites, MOMENT, _no_pause)


def _status(result, site_id="shop"):
    return next(status for status in result.statuses if status.site_id == site_id)


# --- the row ---------------------------------------------------------------------------------------------------------
def test_a_listing_keeps_the_sellers_words_and_an_exact_price():
    row = _run(_shop(_page([_listing()]))).observations[0]
    assert row.listing_text == "Sill & step 1 | Honed edge"
    assert (row.price_amount, row.currency, row.price_status.value) == ("21.25", "EUR", "priced")
    assert row.price_text == "€ 21,25"
    assert row.stated_specs == {"Materiaal": "Hardsteen", "sku": "SKU-1", "weight": "20", "length": "90",
                                "width": "15", "height": "2"}
    assert (row.source_url, row.availability_text) == (f"{BASE}/product/1/", "Ruim op voorraad")
    assert (row.observed_at, row.run_id) == ("2026-11-08T12:30:05Z", "20261108T123005Z")
    assert (row.seller_country, row.served_market) == ("BE", "NL")


def test_what_the_site_does_not_say_stays_unknown():
    row = _run(_shop(_page([_listing()]))).observations[0]
    assert (row.vat_basis.value, row.delivery_included.value) == ("unknown", "unknown")
    assert (row.unit_text, row.delivery_text, row.grade_text, row.min_qty_text) == (None, None, None, None)
    assert {row.provenance[name].value for name in ("vat_basis", "unit_text", "grade_text")} == {"unknown"}
    assert row.provenance["price_amount"].value == "listing"


def test_a_site_statement_applies_and_says_where_it_came_from():
    register = _register(_site(statements={"vat_basis": "incl", "unit_text": "per piece"}))
    row = _run(_shop(_page([_listing()])), register).observations[0]
    assert (row.vat_basis.value, row.unit_text) == ("incl", "per piece")
    assert (row.provenance["vat_basis"].value, row.provenance["unit_text"].value) == ("site_statement", "site_statement")


def test_grade_is_read_only_from_the_registers_label():
    attributes = [{"name": "Kwaliteit", "terms": [{"name": "AB"}]}, {"name": "Merk", "terms": [{"name": "X"}]}]
    register = _register(_site(grade_labels=["kwaliteit"]))
    row = _run(_shop(_page([_listing(attributes=attributes)])), register).observations[0]
    assert row.grade_text == "AB"
    assert row.provenance["grade_text"].value == "listing"


@pytest.mark.parametrize("prices", [
    {"price": "0", "currency_code": "EUR", "currency_minor_unit": 2},
    {"price": "", "currency_code": "EUR", "currency_minor_unit": 2},
    {"price": "1000", "currency_code": "EUR", "currency_minor_unit": 2,
     "price_range": {"min_amount": "1000", "max_amount": "2000"}},
])
def test_no_single_price_is_no_price_and_the_text_is_kept(prices):
    row = _run(_shop(_page([_listing(prices=prices)]))).observations[0]
    assert (row.price_amount, row.price_status.value) == (None, "none")
    assert row.price_text is not None


@pytest.mark.parametrize("raw,minor,expected", [("2125", 2, "21.25"), ("5", 2, "0.05"), ("1500", 0, "1500"),
                                                ("123456", 3, "123.456")])
def test_minor_units_convert_exactly(raw, minor, expected):
    prices = {"price": raw, "currency_code": "GBP", "currency_minor_unit": minor}
    assert _run(_shop(_page([_listing(prices=prices)]))).observations[0].price_amount == expected


def test_a_stated_minimum_above_one_is_kept():
    row = _run(_shop(_page([_listing(add_to_cart={"minimum": 12})]))).observations[0]
    assert row.min_qty_text == "12"


def test_an_unusable_currency_code_is_unknown_not_invented():
    prices = {"price": "100", "currency_code": "euro", "currency_minor_unit": 2}
    row = _run(_shop(_page([_listing(prices=prices)]))).observations[0]
    assert (row.currency, row.price_amount) == (None, "1.00")
    assert row.provenance["currency"].value == "unknown"


# --- isolation ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [
    "not an object",
    _listing(2, name=""),
    _listing(2, id=None),
    _listing(2, permalink="https://elsewhere.example/product/2/"),
    _listing(2, permalink="javascript:alert(1)"),
    _listing(2, permalink="https://evil.example\\@shop.example/x"),
    _listing(2, permalink="https://user@shop.example/x"),
    _listing(2, permalink="https://shop.example/a\nb"),
    _listing(2, prices={"price": "12.50", "currency_code": "EUR", "currency_minor_unit": 2}),
    _listing(2, prices={"price": "100", "currency_code": "EUR", "currency_minor_unit": True}),
    _listing(2, prices=None),
    _listing(2, attributes=7),
    _listing(2, attributes=[{"name": "x", "terms": 7}]),
    _listing(2, add_to_cart="x"),
    _listing(2, stock_availability="x"),
])
def test_one_malformed_listing_marks_the_site_partial_and_the_rest_still_count(bad):
    result = _run(_shop(_page([_listing(1), bad])))
    assert [row.listing_key for row in result.observations] == ["1"]
    assert (_status(result).status.value, _status(result).malformed_listings) == ("partial", 1)


_HOSTILE = [None, True, 7, 1.5, float("nan"), float("inf"), "", "   ", "\ud800", "\uff03", "x" * 200_000, "9" * 5000,
            "https://[bad/", "https://evil.example\\@shop.example/x", "<![if", [], {}, [1], {"a": 1}, [[]], [{"name": 1}]]
_HOSTILE_PATHS = [("id",), ("name",), ("permalink",), ("sku",), ("short_description",), ("price_html",), ("prices",),
                  ("prices", "price"), ("prices", "currency_code"), ("prices", "currency_minor_unit"),
                  ("prices", "price_range"), ("attributes",), ("dimensions",), ("dimensions", "length"), ("weight",),
                  ("stock_availability",), ("stock_availability", "text"), ("add_to_cart",), ("add_to_cart", "minimum")]


@pytest.mark.parametrize("path", _HOSTILE_PATHS, ids=".".join)
def test_no_listing_value_can_lose_the_run(path, tmp_path):
    """Any value in any field: the good listing still ships, and the file can be written and read back."""
    for index, value in enumerate(_HOSTILE):
        bad = json.loads(json.dumps(_listing(2)))
        target = bad
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
        result = _run(_shop(_page([_listing(1), bad])))
        run_dir = write_run(result, tmp_path / str(index))
        assert "1" in {row.listing_key for row in result.observations}
        assert file_entry(run_dir / config.OBSERVATIONS_FILE)["rows"] == len(result.observations)


@pytest.mark.parametrize("field", ["sku", "short_description"])
@pytest.mark.parametrize("value", ["x" * (config.MAX_CELL_CHARS + 1), "\ud800"], ids=["huge", "surrogate"])
def test_a_huge_or_unencodable_cell_is_the_listings_problem_alone(field, value):
    result = _run(_shop(_page([_listing(1), _listing(2, **{field: value})])))
    assert [row.listing_key for row in result.observations] == ["1"]
    assert (_status(result).status.value, _status(result).malformed_listings) == ("partial", 1)


def test_a_repeated_attribute_keeps_both_values():
    attributes = [{"name": "Dikte", "terms": [{"name": "2"}]}, {"name": "Dikte", "terms": [{"name": "3"}]}]
    assert _run(_shop(_page([_listing(attributes=attributes)]))).observations[0].stated_specs["Dikte"] == "2 / 3"


# --- stated_specs completeness: every spec the page states, so matching has thickness, finish, size, format, grade
# and origin. The attributes and the API's own fields, and the "Key: value" items of the description's list (how
# Ceralith states about half its products), each as the seller wrote it.
_DESCRIPTION = ("<p>Een mooie tegel.</p><ul><li><strong>Dikte:</strong> 3 cm</li><li>Afwerking : gezoet</li>"
                "<li>Formaat: 60x60</li><li>Herkomst: Belgi&euml;</li></ul>")


def test_every_stated_spec_is_kept_attributes_api_fields_and_the_description_list():
    attributes = [{"name": "Kwaliteit", "terms": [{"name": "Eerste keus"}]}]
    row = _run(_shop(_page([_listing(attributes=attributes, description=_DESCRIPTION)]))).observations[0]
    assert row.stated_specs == {"Kwaliteit": "Eerste keus", "sku": "SKU-1", "weight": "20", "length": "90",
                                "width": "15", "height": "2", "Dikte": "3 cm", "Afwerking": "gezoet",
                                "Formaat": "60x60", "Herkomst": "België"}


def test_a_description_item_never_overrides_an_attribute_of_the_same_label():
    attributes = [{"name": "Dikte", "terms": [{"name": "2 cm"}]}]
    specs = _run(_shop(_page([_listing(attributes=attributes, description="<ul><li>dikte: 3 cm</li></ul>")])))
    assert {label: value for label, value in specs.observations[0].stated_specs.items()
            if label.lower() == "dikte"} == {"Dikte": "2 cm"}


def test_the_first_description_item_of_a_label_is_kept():
    description = "<ul><li>Dikte: 3 cm</li><li>DIKTE: 4 cm</li></ul>"
    assert _run(_shop(_page([_listing(description=description)]))).observations[0].stated_specs["Dikte"] == "3 cm"


@pytest.mark.parametrize("item", [
    "Geschikt voor binnen en buiten",                       # no label
    "Dikte:", ": 3 cm",                                     # an empty side
    "Let op: " + "lang " * 60,                              # a sentence, not a spec value
    "x" * (config.DESCRIPTION_SPEC_MAX_LABEL_CHARS + 1) + ": 3",
])
def test_a_description_item_that_is_not_a_key_and_value_is_not_a_spec(item):
    row = _run(_shop(_page([_listing(description=f"<ul><li>{item}</li></ul>")]))).observations[0]
    assert row.stated_specs == {"Materiaal": "Hardsteen", "sku": "SKU-1", "weight": "20", "length": "90",
                                "width": "15", "height": "2"}


@pytest.mark.parametrize("description", [None, 7, ["<li>Dikte: 3</li>"], "<p>Dikte: 3 cm</p>"])
def test_only_a_text_descriptions_list_items_are_read(description):
    row = _run(_shop(_page([_listing(description=description)]))).observations[0]
    assert "Dikte" not in row.stated_specs


@pytest.mark.parametrize("unit", ["<li>", "<li ", "<li x"])
def test_a_description_of_unclosed_list_items_is_read_in_bounded_time(unit):
    # a hostile description, about a megabyte: list items that never close, or opening tags that never reach their
    # '>', must not stall the listing (each once took minutes)
    import time
    from market_prices.collectors.woocommerce_store import _list_items
    started = time.monotonic()
    assert _list_items(unit * (1_000_000 // len(unit))) == []
    assert time.monotonic() - started < 2.0


@pytest.mark.parametrize("html", [
    "<ul><li>Dikte: 3</li><LI class='x'>Formaat: 60x60</LI ></ul>",
    "<li>a<li>b</li><li>c</li>", "<link><li>x</li>", "<li>open", "</li><li>y</lix><li>z</li>", "",
])
def test_the_list_scan_reads_items_as_the_html_list_holds_them(html):
    import re
    from market_prices.collectors.woocommerce_store import _list_items
    expected = re.findall(r"<li\b[^>]*>(.*?)</li\s*>", html, re.IGNORECASE | re.DOTALL)
    assert _list_items(html) == expected


def test_a_sellers_less_than_sign_stays_text():
    row = _run(_shop(_page([_listing(name="Slab <3cm and >2cm", short_description="")]))).observations[0]
    assert row.listing_text == "Slab <3cm and >2cm"


def test_a_float_id_is_refused():
    assert _status(_run(_shop(_page([_listing(1), _listing(2, id=1.0)])))).malformed_listings == 1


def test_a_listing_seen_twice_counts_once():
    result = _run(_shop(_page([_listing(1), _listing(2)], total=2), _page([_listing(2, sku="LATER")], total=2)))
    assert [row.listing_key for row in result.observations] == ["1", "2"]
    assert result.observations[1].stated_specs["sku"] == "SKU-2"
    assert (_status(result).status.value, _status(result).duplicate_listings) == ("ok", 1)


# --- the terms gate ----------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("decision", ["pending", "refused"])
def test_a_site_without_approval_is_never_requested(decision):
    sites = _Sites({})
    result = _run(sites, _register(_site(decision=decision, decided_by=None, decided_on=None)))
    assert sites.requested == []
    assert (result.observations, _status(result).status.value) == ((), "disabled_terms")


@pytest.mark.parametrize("change", [
    {"terms": {"checked_on": "2026-11-02", "summary": "x"}},
    {"terms": {"terms_url": f"{BASE}/t", "summary": "x"}},
    {"terms": {"terms_url": f"{BASE}/t", "checked_on": "2026-11-02"}},
    {"decided_by": None},
    {"decided_on": None},
    {"decided_on": "2026-11-01"},
    {"seller_country": None},
    {"served_market": None},
])
def test_approval_is_refused_without_the_full_record(change):
    with pytest.raises(ValueError, match="approved needs"):
        Site.model_validate(_site(**change))


@pytest.mark.parametrize("change", [
    {"base_url": "http://shop.example"},
    {"base_url": "https://shop.example/path"},
    {"site_id": "../evil"},
    {"platform": "unknown"},
    {"query": {"page": "9"}},
    {"seller_type": "friend"},
    {"surprise": 1},
    {"base_url": "https://shop.example?"},
    {"base_url": "https://user@shop.example"},
    {"site_id": "shop\n"},
    {"seller_country": "Belgium"},
    {"decided_by": "   "},
    {"terms": {"terms_url": "n/a", "checked_on": "2026-11-02", "summary": "x"}},
    {"terms": {"terms_url": f"{BASE}/t", "checked_on": "2026-11-02", "summary": "   "}},
    {"terms": {"terms_url": f"{BASE}/t", "checked_on": 0, "summary": "x"}},
    {"base_url": "https://:pw@shop.example"},
    {"terms": {"terms_url": f"{BASE}/t", "checked_on": "2062-01-01", "summary": "x"}, "decided_on": "2062-01-02"},
])
def test_a_bad_site_entry_is_refused(change):
    with pytest.raises(ValueError):
        Site.model_validate(_site(**change))


def test_duplicate_sites_are_refused():
    with pytest.raises(ValueError, match="duplicate site_id"):
        _register(_site(), _site())


def test_shipped_registers_load():
    assert load_register("blokport").brand == "blokport"
    assert load_register("wudport").brand == "wudport"


def test_a_register_problem_raises(tmp_path):
    with pytest.raises(RegisterError, match="must match"):
        load_register("../blokport", tmp_path)
    with pytest.raises(RegisterError, match="no site register"):
        load_register("nobody", tmp_path)
    (tmp_path / "broken.yaml").write_text("brand: [", encoding="utf-8")
    with pytest.raises(RegisterError, match="not valid YAML"):
        load_register("broken", tmp_path)
    (tmp_path / "other.yaml").write_text(yaml.safe_dump({"brand": "else", "sites": []}), encoding="utf-8")
    with pytest.raises(RegisterError, match="declares brand"):
        load_register("other", tmp_path)
    (tmp_path / "shape.yaml").write_text("sites: 3\n", encoding="utf-8")
    with pytest.raises(RegisterError, match="does not validate"):
        load_register("shape", tmp_path)


# --- robots ------------------------------------------------------------------------------------------------------------
def test_robots_that_forbid_the_api_stop_the_site_before_any_page():
    sites = _shop(robots=Response(200, "User-agent: *\nDisallow: /wp-json/\n"))
    result = _run(sites)
    assert sites.requested == [ROBOTS]
    assert _status(result).status.value == "disallowed_robots"
    assert "robots.txt disallows" in _status(result).detail


def test_absent_robots_allow_even_when_the_404_page_loads_a_captcha_script():
    robots = Response(404, '<html><script src="recaptcha/api.js"></script></html>', {"Content-Type": "text/html"})
    assert _status(_run(_shop(_page([_listing()]), robots=robots))).status.value == "ok"


@pytest.mark.parametrize("robots", [Response(500, ""), Response(200, "<html>Welcome</html>")])
def test_unknown_robots_fail_closed(robots):
    sites = _shop(robots=robots)
    result = _run(sites)
    assert sites.requested == [ROBOTS]
    assert _status(result).status.value == "unavailable_error"


def test_a_robots_rule_that_mentions_a_marker_is_not_a_block():
    robots = Response(200, "User-agent: *\nDisallow: /captcha/\n", {"Content-Type": "text/plain"})
    assert _status(_run(_shop(_page([_listing()]), robots=robots))).status.value == "ok"


_TARGET = "/wp-json/wc/store/v1/products?page=1&per_page=100"


@pytest.mark.parametrize("text,allowed", [
    ("User-agent: *\nDisallow:\n", True),
    ("User-agent: *\nDisallow: /wp-json/\n", False),
    ("User-agent: *\nDisallow: /*?\n", False),
    ("User-agent: *\nDisallow: /*wc/store*\n", False),
    ("User-agent: *\nDisallow: /*per_page=\n", False),
    ("User-agent: *\nDisallow: /wp-json/wc/store/v1/products?page=1\n", False),
    ("User-agent: *\nDisallow: /*100$\n", False),
    ("User-agent: *\nDisallow: /*99$\n", True),
    ("User-agent: *\nAllow: /\nDisallow: /wp-json/\n", False),
    ("User-agent: *\nDisallow: /wp-json/\nAllow: /wp-json/wc/\n", True),
    ("User-agent: *\nAllow: /wp-json/\nDisallow: /wp-json/\n", True),
    ("User-agent: *\nAllow: /a\n\nUser-agent: *\nDisallow: /wp-json/\n", False),
    ("User-agent: other\nDisallow: /\n", True),
    ("User-agent: other\nUser-agent: *\nDisallow: /\n", False),
    (f"User-agent: {config.ROBOTS_TOKEN.upper()}\nDisallow: /wp-json/\n\nUser-agent: *\nAllow: /\n", False),
    (f"User-agent: {config.ROBOTS_TOKEN}\nAllow: /\n\nUser-agent: *\nDisallow: /\n", True),
    (f"User-agent: {config.ROBOTS_TOKEN}\n\nUser-agent: *\nDisallow: /\n", False),
    (f"User-agent: {config.ROBOTS_TOKEN}\nAllow:\n\nUser-agent: *\nDisallow: /\n", True),
    ("User-agent: *\nDisallow: /wp%2Djson/\n", False),
    ("# only a comment\nnonsense\nUser-agent: * # all\nDisallow: /wp-json/ # no\n", False),
    ("User-agent: *\nDisallow: wp-json/\n", False),
    ("\ufeffUser-agent: *\nDisallow: /\n", False),
    ("User-agent: *\nDisallow: /\nAllow: /%2A\n", False),
    ("User-agent: *\nDisallow: /\nAllow: /a%24\n", False),
    ("User-agent: *\nDisallow: /\nAllow: /wp-json/wc/store/v1/products?page=1&per_page=1%30%30\n", True),
    (f"User-agent: {config.ROBOTS_TOKEN}/1.0\nDisallow: /\n\nUser-agent: *\nAllow: /\n", False),
])
def test_robots_rules_follow_the_standard(text, allowed):
    assert parse_robots(text, config.ROBOTS_TOKEN).allows(_TARGET) is allowed


@pytest.mark.parametrize("rule,target,allowed", [
    ("Disallow: /a%2Ab", "/a*b", False),
    ("Disallow: /a%2Ab", "/axxb", True),
    ("Disallow: /price$", "/price", False),
    ("Disallow: /price%24", "/price$", False),
    ("Disallow: /price%24", "/price", True),
    ("Disallow: /a$b", "/a$b", False),
    ("Disallow: /a$b", "/ab", True),
])
def test_a_literal_star_or_dollar_stays_literal(rule, target, allowed):
    assert parse_robots(f"User-agent: *\n{rule}\n", config.ROBOTS_TOKEN).allows(target) is allowed


def test_a_robots_file_of_wildcards_stays_fast():
    pattern = "*a" * 2000
    rules = parse_robots(f"User-agent: *\nDisallow: /{pattern}b\n", config.ROBOTS_TOKEN)
    assert rules.allows("/" + "a" * 4000) is True


@pytest.mark.parametrize("value,expected", [("7", 7.0), ("nan", 0.0), ("inf", 0.0), ("-3", 0.0), ("soon", 0.0)])
def test_crawl_delay_is_read_only_when_usable(value, expected):
    assert parse_robots(f"User-agent: *\nCrawl-delay: {value}\n", config.ROBOTS_TOKEN).crawl_delay == expected


def test_requests_are_paced_by_the_site_or_our_floor():
    for robots, expected in ((ROBOTS_OPEN, config.MIN_REQUEST_INTERVAL_SECONDS),
                             (Response(200, "User-agent: *\nCrawl-delay: 30\n"), 30.0)):
        pauses: list[float] = []
        collect_run(_register(), _shop(_page([_listing(1)], total=2), _page([_listing(2)], total=2), robots=robots),
                    MOMENT, pauses.append)
        assert pauses == [expected, expected]


def test_a_crawl_delay_over_the_cap_stops_the_site():
    sites = _shop(robots=Response(200, f"User-agent: *\nCrawl-delay: {config.MAX_CRAWL_DELAY_SECONDS + 1}\n"))
    result = _run(sites)
    assert sites.requested == [ROBOTS]
    assert _status(result).status.value == "disallowed_robots"


def test_an_oversized_robots_file_fails_closed():
    sites = _shop(robots=Response(200, "User-agent: *\nAllow: /\n" + "#" * config.MAX_ROBOTS_CHARS))
    assert _status(_run(sites)).status.value == "unavailable_error"
    assert sites.requested == [ROBOTS]


# --- a block is reported, never worked around ----------------------------------------------------------------------------
@pytest.mark.parametrize("answer", [
    Response(403, "denied"),
    Response(401, "login"),
    Response(429, "slow down", {"Retry-After": "1"}),
    Response(406, "not acceptable"),
    Response(451, "legal"),
    Response(503, "<html>Your access to this site has been limited by Wordfence</html>"),
    Response(200, "<html>Sucuri Website Firewall - Access Denied</html>"),
    Response(503, "", {"cf-mitigated": "challenge"}),
    Response(200, "<html><title>Just a moment...</title></html>", {"Content-Type": "text/html"}),
])
def test_a_block_ends_the_site_with_no_rows_and_no_retry(answer):
    sites = _shop(_page([_listing(1)], total=3), answer)
    result = _run(sites)
    assert sites.requested == [ROBOTS, _page_url(1), _page_url(2)]
    assert (result.observations, _status(result).rows) == ((), 0)
    assert _status(result).status.value == "unavailable_blocked"
    assert _status(result).detail


def test_a_blocked_robots_file_blocks_the_site():
    assert _status(_run(_shop(robots=Response(403, "")))).status.value == "unavailable_blocked"


def test_json_that_mentions_a_marker_is_not_a_block():
    result = _run(_shop(_page([_listing(name="Just a moment bench")])))
    assert _status(result).status.value == "ok"


def test_a_blocked_site_leaves_the_others_running():
    other = "https://other.example"
    sites = _Sites({ROBOTS: Response(403, ""), f"{other}/robots.txt": ROBOTS_OPEN,
                    _page_url(1, other): _page([_listing(permalink=f"{other}/p/1/")])})
    result = _run(sites, _register(_site("shop"), _site("other", base_url=other)))
    assert (_status(result).status.value, _status(result, "other").status.value) == ("unavailable_blocked", "ok")
    assert [row.site_id for row in result.observations] == ["other"]


# --- answers that are not usable -------------------------------------------------------------------------------------
@pytest.mark.parametrize("first", [
    Response(500, ""),
    Response(200, "not json", {"X-WP-TotalPages": "1", "Content-Type": "application/json"}),
    Response(200, "{}", {"X-WP-TotalPages": "1", "Content-Type": "application/json"}),
    Response(200, "[]", {"Content-Type": "application/json"}),
    Response(200, "[]", {"X-WP-TotalPages": "many", "Content-Type": "application/json"}),
    Response(200, "[]", {"X-WP-TotalPages": str(config.WOO_MAX_PAGES + 1), "Content-Type": "application/json"}),
    Response(200, "[]", {"X-WP-TotalPages": "9" * 5000, "Content-Type": "application/json"}),
    Response(200, "[" + "1" * 5000 + "]", {"X-WP-TotalPages": "1", "Content-Type": "application/json"}),
    Response(200, "[" * 200000, {"X-WP-TotalPages": "1", "Content-Type": "application/json"}),
    TransportError("timeout"),
])
def test_an_unusable_first_page_is_an_error_with_no_rows(first):
    result = _run(_shop(first))
    assert (result.observations, _status(result).status.value) == ((), "unavailable_error")
    assert _status(result).detail


def test_an_empty_catalogue_is_an_error_not_a_clean_run():
    assert _status(_run(_shop(_page([], total=0)))).status.value == "unavailable_error"


def test_a_site_of_only_malformed_listings_is_an_error():
    result = _run(_shop(_page(["x", "y"])))
    assert (_status(result).status.value, _status(result).malformed_listings) == ("unavailable_error", 2)


def test_a_homepage_that_loads_recaptcha_is_an_error_not_a_block():
    page = Response(200, '<html><head><script src="https://www.google.com/recaptcha/api.js"></script></head></html>',
                    {"Content-Type": "text/html"})
    assert _status(_run(_shop(page))).status.value == "unavailable_error"


def test_a_json_page_with_no_content_type_that_mentions_a_marker_is_not_a_block():
    page = Response(200, json.dumps([_listing(name="Captcha bench")]), {"X-WP-TotalPages": "1"})
    assert _status(_run(_shop(page))).status.value == "ok"


def test_a_site_lowering_its_page_count_midway_is_not_a_clean_read():
    sites = _shop(_page([_listing(1)], total=3), _page([_listing(2)], total=1), Response(400, "out of range"))
    result = _run(sites)
    assert len(sites.requested) == 4
    assert _status(result).status.value == "partial"


def test_a_later_page_failing_keeps_what_was_read():
    result = _run(_shop(_page([_listing(1)], total=2), TransportError("reset")))
    assert [row.listing_key for row in result.observations] == ["1"]
    assert _status(result).status.value == "partial"
    assert "stopped early" in _status(result).detail


def test_pages_are_followed_and_the_query_is_kept():
    url = f"{BASE}{config.WOO_STORE_PATH}?page=1&per_page={config.WOO_PAGE_SIZE}&type=variation"
    sites = _Sites({ROBOTS: ROBOTS_OPEN, url: _page([_listing()])})
    assert _status(_run(sites, _register(_site(query={"type": "variation"})))).status.value == "ok"


# --- the files ---------------------------------------------------------------------------------------------------------
def _pair_of_sites():
    other = "https://other.example"
    answers = {ROBOTS: ROBOTS_OPEN, _page_url(1): _page([_listing(10), _listing(9)]),
               f"{other}/robots.txt": ROBOTS_OPEN, _page_url(1, other): _page([_listing(1, permalink=f"{other}/p/1/")])}
    return answers, _register(_site("zeta"), _site("alpha", base_url=other))


def test_files_are_sorted_and_byte_identical_on_a_re_run(tmp_path):
    answers, register = _pair_of_sites()
    first = write_run(collect_run(register, _Sites(answers), MOMENT, _no_pause), tmp_path / "a")
    second = write_run(collect_run(register, _Sites(answers), MOMENT, _no_pause), tmp_path / "b")
    for name in (config.OBSERVATIONS_FILE, config.SITE_STATUS_FILE):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    rows = list(csv.DictReader(io.StringIO((first / config.OBSERVATIONS_FILE).read_text(encoding="utf-8"))))
    assert [(row["site_id"], row["listing_key"]) for row in rows] == [("alpha", "1"), ("zeta", "10"), ("zeta", "9")]
    assert first.parts[-2:] == ("blokport", "20261108T123005Z")


def test_csv_cells_are_what_the_contract_says(tmp_path):
    run_dir = write_run(_run(_shop(_page([_listing()]))), tmp_path)
    row = next(csv.DictReader((run_dir / config.OBSERVATIONS_FILE).open(encoding="utf-8", newline="")))
    assert row["contract_version"] == config.CONTRACT_VERSION
    assert row["unit_text"] == ""
    assert json.loads(row["stated_specs"])["Materiaal"] == "Hardsteen"
    assert json.loads(row["provenance"])["vat_basis"] == "unknown"


def test_a_run_moment_must_be_timezone_aware():
    with pytest.raises(ValueError, match="timezone-aware"):
        stamp_for(datetime(2026, 11, 8))


def test_the_run_id_is_utc():
    assert stamp_for(datetime(2026, 11, 8, 1, 0, tzinfo=timezone(timedelta(hours=2)))).run_id == "20261107T230000Z"


# --- delivery ----------------------------------------------------------------------------------------------------------
class _Body:
    def __init__(self, data: bytes):
        self.data = data

    def read(self) -> bytes:
        return self.data


class _S3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, str] = {}
        self.order: list[str] = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.objects[Key])}

    def head_object(self, Bucket, Key):
        return {"VersionId": self.versions[Key]}

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()
        self.versions[Key] = f"v{len(self.order)}"
        self.order.append(Key)
        return {"VersionId": self.versions[Key]}


def _published(tmp_path, s3, moment=MOMENT, sites=None):
    result = collect_run(_register(), sites or _shop(_page([_listing()])), moment, _no_pause)
    return publish_run(s3, "bucket", "blokport", write_run(result, tmp_path))


def test_a_run_is_delivered_files_then_manifest_then_latest(tmp_path):
    s3 = _S3()
    outcome = _published(tmp_path, s3)
    prefix = "dev/market_prices/blokport"
    assert s3.order == [f"{prefix}/20261108T123005Z/observations.csv", f"{prefix}/20261108T123005Z/site_status.csv",
                        f"{prefix}/20261108T123005Z/manifest.json", f"{prefix}/latest.json"]
    assert outcome["latest_moved"] is True
    assert outcome["manifest"]["files"]["observations.csv"]["rows"] == 1
    latest = json.loads(s3.objects[f"{prefix}/latest.json"])
    assert latest == {"brand": "blokport", "contract_version": config.CONTRACT_VERSION, "rows": 1,
                      "run_id": "20261108T123005Z", "manifest_key": f"{prefix}/20261108T123005Z/manifest.json"}


def test_a_late_publish_of_an_older_run_does_not_move_latest(tmp_path):
    s3 = _S3()
    _published(tmp_path, s3)
    outcome = _published(tmp_path, s3, MOMENT - timedelta(days=1))
    assert outcome["latest_moved"] is False
    assert json.loads(s3.objects["dev/market_prices/blokport/latest.json"])["run_id"] == "20261108T123005Z"


def test_a_run_with_no_rows_is_delivered_as_evidence_but_leaves_latest(tmp_path):
    s3 = _S3()
    _published(tmp_path, s3)
    later = MOMENT + timedelta(days=1)
    outcome = _published(tmp_path, s3, later, _shop(robots=Response(403, "")))
    assert outcome["latest_moved"] is False
    assert "dev/market_prices/blokport/20261109T123005Z/site_status.csv" in s3.objects
    assert json.loads(s3.objects["dev/market_prices/blokport/latest.json"])["run_id"] == "20261108T123005Z"


def test_a_folder_missing_a_file_is_refused_before_anything_is_written(tmp_path):
    s3 = _S3()
    run_dir = tmp_path / "blokport" / "20261108T123005Z"
    run_dir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        publish_run(s3, "bucket", "blokport", run_dir)
    assert s3.order == []


@pytest.mark.parametrize("brand,parts", [("blokport", ("blokport", "zzzz")),
                                         ("blokport", ("wudport", "20261108T123005Z")),
                                         ("..", ("a", "..", "20261108T123005Z"))])
def test_a_folder_that_is_not_this_brands_run_is_refused(tmp_path, brand, parts):
    s3 = _S3()
    with pytest.raises(ValueError, match="not a run folder"):
        publish_run(s3, "bucket", brand, tmp_path.joinpath(*parts))
    assert s3.order == []


# --- the boundary ------------------------------------------------------------------------------------------------------
_PACKAGE = Path(market_prices.__file__).parent
# the shared delivery and settings; the scraper's one SSRF-checked redirect walk (the transport) and its AWS client
# factory (the run entry)
# scrapers.run: the product scraper's suppliers, for the supplier flag (register.supplier_keys)
# stone_pipeline.core.env: the repo's one env reader, for the run input naming the run's brand (run_input.run_brand)
# stone_pipeline.core.text: the one shared name folding (match_key), which a vocabulary word is compared by; pure, no I/O
_ALLOWED_OUTSIDE = {"stone_pipeline.io.deliverables", "stone_pipeline.config.settings", "stone_pipeline.io.ssrf",
                    "stone_pipeline.io.storage", "scrapers.run", "stone_pipeline.core.env",
                    "stone_pipeline.core.text"}
# openpyxl reads the supplied .xlsx price sheets; botocore's ClientError tells the intake record's absence from a failure
_THIRD_PARTY = {"pydantic", "yaml", "httpx", "openpyxl", "openpyxl.utils.exceptions", "botocore.exceptions"}
_NETWORK = {"urllib.request", "http.client", "socket", "ssl", "ftplib", "smtplib"}
_HTTP_CLIENTS = {"httpx", "requests", "curl_cffi", "aiohttp", "urllib3"}


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names} & _NETWORK
    return found


def test_the_package_touches_only_the_shared_delivery_and_the_one_redirect_walk():
    everything = set().union(*(_imports(path) for path in _PACKAGE.rglob("*.py")))
    assert not everything & _NETWORK
    assert {name for name in everything if name.split(".")[0] != "market_prices"
            and name.split(".")[0] not in sys.stdlib_module_names} == _ALLOWED_OUTSIDE | _THIRD_PARTY


def test_only_the_live_transport_holds_an_http_client():
    holders = {path.relative_to(_PACKAGE).as_posix() for path in _PACKAGE.rglob("*.py")
               if {name.split(".")[0] for name in _imports(path)} & _HTTP_CLIENTS}
    assert holders == {"transport.py"}


# --- the run's scope: its brand's vocabulary, asked once per listing after the fetch (W4, Mas 13 Nov) -----------------
def _wudport_register() -> Register:
    return Register.model_validate({"brand": "wudport", "sites": [_site(brand="wudport", served_market="GB")]})


def test_a_listing_outside_the_brands_scope_is_not_delivered_and_is_counted():
    result = _run(_shop(_page([_listing(1, name="Birch plywood 18mm 2440 x 1220"),
                               _listing(2, name="Paint roller 9 inch")])), _wudport_register())
    assert [row.listing_key for row in result.observations] == ["1"]
    status = _status(result)
    assert (status.status.value, status.rows) == ("ok", 1)
    assert "listings out of scope x1" in status.detail


def test_scope_is_decided_before_a_listing_is_read_so_an_out_of_scope_one_is_never_malformed():
    """f8's note: an out-of-scope listing that would not read (prices not an object) is out of scope, not malformed,
    so it can never turn a site partial; malformed counts in-scope listings only."""
    result = _run(_shop(_page([_listing(1, name="Birch plywood 18mm 2440 x 1220"),
                               _listing(2, name="Paint roller 9 inch", prices="unreadable")])), _wudport_register())
    status = _status(result)
    assert (status.status.value, status.rows, status.malformed_listings) == ("ok", 1, 0)
    assert status.detail == "listings out of scope x1"


def test_a_site_with_no_listing_in_scope_is_read_whole_and_delivers_none():
    result = _run(_shop(_page([_listing(1, name="Paint roller 9 inch")])), _wudport_register())
    status = _status(result)
    assert (status.status.value, status.rows, result.observations) == ("ok", 0, ())
    assert "listings out of scope x1" in status.detail


def test_a_site_in_a_market_the_vocabulary_does_not_declare_stops_the_run_before_any_fetch():
    # WudPort's vocabulary declares GB DE FR NL BE PL SE; a collectable site serving US is a setup error, named
    from market_prices.vocabulary import VocabularyError
    sites = _shop(_page([_listing(1, name="Birch plywood 18mm")]))
    register = Register.model_validate({"brand": "wudport", "sites": [_site(brand="wudport", served_market="US")]})
    with pytest.raises(VocabularyError, match="shop"):
        _run(sites, register)
    assert sites.requested == []


def test_a_scope_all_brand_delivers_every_listing_as_before():
    result = _run(_shop(_page([_listing(1), _listing(2, name="Paint roller 9 inch")])))
    assert [row.listing_key for row in result.observations] == ["1", "2"]
    assert (_status(result).status.value, _status(result).detail) == ("ok", "")
