"""The generic declared-HTML collector (platform declared_html; Mas approved 14 Nov, plan
blokport_research/2026-11-14-seed-declared-html-plan.md), on fixtures only. A site's register row DECLARES where its
facts sit in its pages (a record selector, and per field a JSON attribute path, an attribute, or a text, each with an
optional pattern); one record is one listing. The fixtures copy the shapes worker 74 measured: a card grid with an
analytics JSON attribute and a visible price (Country Floors), per-thickness variation rows on product pages
(Mandarin Stone), and sale cards with a struck and a current price (Starel Stones). Nothing reaches a real site."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from market_prices.collect import collect_run
from market_prices.fetching import Response
from market_prices.register import Register, load_register
from market_prices.schema import PriceStatus

MOMENT = datetime(2026, 11, 14, 9, 0, 0, tzinfo=timezone.utc)
BASE = "https://shop.example"
ROBOTS = f"{BASE}/robots.txt"
ROBOTS_OPEN = Response(200, "User-agent: *\nDisallow:\n")
HTML_TYPE = {"Content-Type": "text/html; charset=utf-8"}

# --- the three declarations, as a register row writes them ------------------------------------------------------------
CARDS = {
    "read": "listing_pages",
    "record": {"class": "items-loop__item"},
    "fields": {
        "listing_key": {"json": ["data-ms-tracking-item", "item_id"]},
        "listing_text": {"json": ["data-ms-tracking-item", "item_name"]},
        "url": {"class": "stretched-link", "attr": "href"},
        "price": {"class": "items-loop__item-price", "text": True, "pattern": r"\$\s*([\d,]+\.\d{2})"},
        "unit": {"class": "sale-unit", "text": True, "pattern": r"per\s+(\S+)"},
        "currency_symbols": {"$": "USD"},
    },
    "number": {"decimal": ".", "thousands": ","},
    "specs": {"Material": {"json": ["data-ms-tracking-item", "item_category2"]}},
}
ROWS = {
    "read": "product_pages",
    "record": {"attribute": "data-variation-product-vars"},
    "fields": {
        "listing_key": {"json": ["data-variation-product-vars", "variation_id"]},
        "listing_text": {"json": ["data-variation-product-vars", "full_title"]},
        "price": {"class": "price-m2-col", "text": True, "pattern": r"£\s*([\d,]+\.\d{2})"},
        "currency_symbols": {"£": "GBP"},
    },
    "number": {"decimal": ".", "thousands": ","},
    "specs": {"Thickness": {"json": ["data-variation-product-vars", "title"], "pattern": r"(\d+\s*mm)"},
              "Price ex VAT": {"json": ["data-variation-product-vars", "regular_price_exvat"]}},
}
SALE = {
    "read": "listing_pages",
    "record": {"tag": "article", "class": "starel-loop-card"},
    "fields": {
        "listing_key": {"class": "card-link", "attr": "href"},
        "listing_text": {"class": "card-title", "text": True},
        "url": {"class": "card-link", "attr": "href"},
        "price": {"class": "starel-loop-card-price__row--sqm", "text": True, "pattern": r"£\s*([\d,]+\.\d{2})"},
        "currency_symbols": {"£": "GBP"},
    },
    "number": {"decimal": ".", "thousands": ","},
    "specs": {"Price per slab": {"class": "starel-loop-card-price__row--slab", "text": True},
              # Mas's sale rule (14 Nov): the struck "was" price is kept, read explicitly, never as the price
              "Regular price": {"class": "starel-loop-card-price__row--sqm", "text": True, "struck": True}},
}


def _site(declaration: dict, discovery=None, **overrides) -> dict:
    site = {
        "brand": "blokport", "site_id": "shop", "seller_name": "Shop", "seller_type": "merchant",
        "seller_country": "GB", "served_market": "GB", "platform": "declared_html", "base_url": BASE,
        "discovery": discovery or {"listing_pages": ["/collections/slabs/"],
                                   "product_path": r"^/product/[a-z0-9-]+/?$"},
        "html": declaration,
        "terms": {"terms_url": f"{BASE}/terms", "checked_on": "2026-11-14", "summary": "No clause on copying."},
        "decision": "approved", "decided_by": "owner", "decided_on": "2026-11-14",
    }
    return {**site, **overrides}


def _register(*sites: dict) -> Register:
    return Register.model_validate({"brand": "blokport", "sites": list(sites)})


class _Sites:
    """A scripted transport. Any URL it was not told about fails the test: nothing is fetched by accident."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.requested: list[str] = []

    def get(self, url: str) -> Response:
        self.requested.append(url)
        return self.answers[url]


def _run(answers: dict, site: dict):
    return collect_run(_register(site), _Sites({ROBOTS: ROBOTS_OPEN, **answers}), MOMENT, lambda _seconds: None)


def _rows(result) -> dict:
    return {row.listing_key: row for row in result.observations}


def _status(result):
    return next(status for status in result.statuses if status.site_id == "shop")


def _page(body: str) -> Response:
    return Response(200, f"<!doctype html><html><head><title>t</title></head><body>{body}</body></html>", HTML_TYPE)


def _attr(data: dict) -> str:
    return html.escape(json.dumps(data), quote=True)            # as the site writes it: &quot; inside "..."


def _card(item_id: str, name: str, price_html: str, analytics_price: float = 39.2) -> str:
    data = {"item_id": item_id, "item_name": name, "item_category2": "Marble", "price": analytics_price,
            "sale_unit": "sqft"}
    return (f'<div class="items-loop__item" data-ms-tracking-item="{_attr(data)}">'
            f'<a class="stretched-link" href="/product/{item_id.lower()}/">{name}</a>'
            f'<div class="items-loop__item-title">{name}</div>'
            f'<div class="items-loop__item-price">{price_html}</div></div>')


PRICED = ('<div class="prices"><span class="price"><span class="woocommerce-Price-currencySymbol">&#36;</span>39.20'
          '</span><span class="sale-unit">/ per sqft</span></div><div class="order-sample">Order Swatch</div>')
UNPRICED = '<div class="order-sample"><a class="btn" href="/product/x/">View Product</a></div>'


def _variation(variation_id: int, title: str, shown: str, exvat: str) -> str:
    data = {"variation_id": variation_id, "title": title, "full_title": f"Carrara Honed Marble Slab - {title}",
            "regular_price": shown.lstrip("£"), "regular_price_exvat": exvat}
    return (f'<div class="product-variation-row" data-product-title="{title}" '
            f'data-variation-product-vars="{_attr(data)}"><div class="size-col">{title}</div>'
            f'<div class="price-m2-col">{shown}</div></div>')


# --- a card grid read on its listing pages (the Country Floors shape) ------------------------------------------------
def test_each_card_is_one_listing_its_visible_price_read_and_its_page_followed():
    page_1 = _page(_card("SL1", "Negro Marquina Polished Marble Slab Random", PRICED)
                   + f'<a rel="next" href="{BASE}/collections/slabs/page/2/">next</a>')
    page_2 = _page(_card("SL2", "Port Laurent Polished Marble Slab Random", PRICED))
    result = _run({f"{BASE}/collections/slabs/": page_1, f"{BASE}/collections/slabs/page/2/": page_2}, _site(CARDS))
    rows = _rows(result)
    assert sorted(rows) == ["SL1", "SL2"]
    row = rows["SL1"]
    assert (row.listing_text, row.source_url) == ("Negro Marquina Polished Marble Slab Random",
                                                  f"{BASE}/product/sl1/")
    assert (row.price_amount, row.currency, row.unit_text, row.price_status) == ("39.20", "USD", "sqft",
                                                                                 PriceStatus.priced)
    assert row.price_text == "$ 39.20 / per sqft Order Swatch"          # the element's text, as shown
    assert row.stated_specs == {"Material": "Marble"}
    assert _status(result).status.value == "ok"


def test_a_card_that_shows_no_price_is_unpriced_with_its_words_kept_never_the_analytics_price():
    # Mas's F1 ruling: the analytics JSON's "price" is tracking data, never a stated price
    page = _page(_card("SL3", "Albatre Blanc Polished Quartzite Slab Random", UNPRICED, analytics_price=67.2))
    row = _rows(_run({f"{BASE}/collections/slabs/": page}, _site(CARDS)))["SL3"]
    assert (row.price_amount, row.price_status, row.price_text) == (None, PriceStatus.none, "View Product")


def test_a_declared_on_request_word_makes_an_unpriced_card_on_request():
    declaration = {**CARDS, "fields": {**CARDS["fields"], "on_request": ["Contact us for pricing"]}}
    page = _page(_card("SL4", "Calacatta Oro Slab", '<div class="order-sample">Contact us for pricing</div>'))
    row = _rows(_run({f"{BASE}/collections/slabs/": page}, _site(declaration)))["SL4"]
    assert (row.price_amount, row.price_status, row.price_text) == (None, PriceStatus.on_request,
                                                                    "Contact us for pricing")


# --- per-thickness rows read on product pages (the Mandarin Stone shape) ---------------------------------------------
def test_each_thickness_row_is_its_own_listing_priced_as_shown_with_the_ex_vat_price_kept_as_stated():
    listing = _page('<a href="/product/carrara-honed-marble-slab/">Carrara</a><a href="/about/">About</a>')
    product = _page(_variation(4427, "Slab 20mm", "£342.00", "285.00") + _variation(4428, "Slab 30mm", "£354.00",
                                                                                    "295.00"))
    site = _site(ROWS, statements={"vat_basis": "incl", "unit_text": "m²"})
    result = _run({f"{BASE}/collections/slabs/": listing, f"{BASE}/product/carrara-honed-marble-slab/": product}, site)
    rows = _rows(result)
    assert sorted(rows) == ["4427", "4428"]
    row = rows["4427"]
    assert (row.listing_text, row.source_url) == ("Carrara Honed Marble Slab - Slab 20mm",
                                                  f"{BASE}/product/carrara-honed-marble-slab/")
    assert (row.price_amount, row.currency, row.vat_basis.value, row.unit_text) == ("342.00", "GBP", "incl", "m²")
    assert row.stated_specs == {"Thickness": "20mm", "Price ex VAT": "285.00"}
    assert rows["4428"].price_amount == "354.00"


def test_a_product_page_without_a_declared_record_is_counted_with_its_reason():
    listing = _page('<a href="/product/a/">A</a><a href="/product/b/">B</a>')
    good = _page(_variation(1, "Slab 20mm", "£342.00", "285.00"))
    result = _run({f"{BASE}/collections/slabs/": listing, f"{BASE}/product/a/": good,
                   f"{BASE}/product/b/": _page("<p>No rows here</p>")}, _site(ROWS))
    assert sorted(_rows(result)) == ["1"]
    status = _status(result)
    assert (status.status.value, status.malformed_listings) == ("partial", 1)
    assert "the product page holds no declared record x1" in status.detail


# --- sale cards: the struck price is never read (the Starel Stones shape) --------------------------------------------
def test_struck_text_is_never_the_price_but_is_kept_as_the_regular_price_with_the_sale_flag():
    # Mas's sale rule (14 Nov): the price is the current one as shown; the struck "was" price and "On sale" are kept
    card = ('<article class="starel-loop-card"><a class="card-link" href="/p/arabescato/">x</a>'
            '<h3 class="card-title">Arabescato Marble Slab</h3>'
            '<div class="starel-loop-card-price__row--slab"><del>£2,400.00</del> <ins>£1,999.00</ins> per slab</div>'
            '<div class="starel-loop-card-price__row--sqm"><del>£1,450.00</del> <ins>£1,375.00</ins> per m²</div>'
            '</article>')
    row = _rows(_run({f"{BASE}/collections/slabs/": _page(card)}, _site(SALE)))["/p/arabescato/"]
    assert (row.price_amount, row.price_text) == ("1375.00", "£1,375.00 per m²")
    assert row.stated_specs == {"Price per slab": "£1,999.00 per slab", "Regular price": "£1,450.00",
                                "On sale": "yes"}


def test_a_card_not_on_sale_has_no_regular_price_and_no_sale_flag():
    card = ('<article class="starel-loop-card"><a class="card-link" href="/p/bianco/">x</a>'
            '<h3 class="card-title">Bianco Carrara Marble Slab</h3>'
            '<div class="starel-loop-card-price__row--sqm">£295.00 per m²</div></article>')
    row = _rows(_run({f"{BASE}/collections/slabs/": _page(card)}, _site(SALE)))["/p/bianco/"]
    assert (row.price_amount, row.stated_specs) == ("295.00", {})


def test_a_class_lookup_skips_struck_subtrees_so_the_current_amount_is_read():
    # the manager's ruling 2: a price whose struck and current amounts carry one class reads the current one
    declaration = {**SALE, "fields": {**SALE["fields"], "price": {"class": "amount", "text": True,
                                                                  "pattern": r"£\s*([\d,]+\.\d{2})"}},
                   "specs": {"Regular price": {"class": "amount", "text": True, "struck": True}}}
    card = ('<article class="starel-loop-card"><a class="card-link" href="/p/nero/">x</a>'
            '<h3 class="card-title">Nero Marquina Marble Slab</h3><p class="price">'
            '<del><span class="amount">£1,450.00</span></del> <ins><span class="amount">£1,040.00</span></ins></p>'
            '</article>')
    row = _rows(_run({f"{BASE}/collections/slabs/": _page(card)}, _site(declaration)))["/p/nero/"]
    assert (row.price_amount, row.price_text) == ("1040.00", "£1,040.00")
    assert row.stated_specs == {"Regular price": "£1,450.00", "On sale": "yes"}


def test_a_price_is_read_in_the_declared_notation_and_one_that_does_not_parse_is_unpriced():
    # the manager's ruling 5: the declared separators read a grouped price; another notation is no price
    euro = {**SALE, "fields": {**SALE["fields"], "price": {"class": "starel-loop-card-price__row--sqm", "text": True,
                                                           "pattern": r"([\d.,]+)\s*€"},
                               "currency_symbols": {"€": "EUR"}},
            "number": {"decimal": ",", "thousands": "."}}
    cards = ('<article class="starel-loop-card"><a class="card-link" href="/p/a/">x</a>'
             '<h3 class="card-title">A Marble Slab</h3><div class="starel-loop-card-price__row--sqm">1.040,50 € / m²'
             '</div></article><article class="starel-loop-card"><a class="card-link" href="/p/b/">x</a>'
             '<h3 class="card-title">B Marble Slab</h3><div class="starel-loop-card-price__row--sqm">1,040.50 € / m²'
             '</div></article>')
    rows = _rows(_run({f"{BASE}/collections/slabs/": _page(cards)}, _site(euro)))
    assert (rows["/p/a/"].price_amount, rows["/p/a/"].currency) == ("1040.50", "EUR")
    assert (rows["/p/b/"].price_amount, rows["/p/b/"].price_status, rows["/p/b/"].price_text) == (
        None, PriceStatus.none, "1,040.50 € / m²")


def test_a_spec_whose_pattern_does_not_match_is_left_empty_never_a_refusal():
    # the manager's ruling 3: only the required fields refuse a listing; a spec not in its declared form is not stated
    product = _page(_variation(9, "Offcut", "£120.00", "100.00"))
    listing = _page('<a href="/product/offcut/">Offcut</a>')
    row = _rows(_run({f"{BASE}/collections/slabs/": listing, f"{BASE}/product/offcut/": product}, _site(ROWS)))["9"]
    assert row.stated_specs == {"Price ex VAT": "100.00"}


# --- a LABELLED source: the value a page pairs with a label (the manager's ruling, 14 Nov ~14:40Z) ------------------
LABELLED = {**SALE, "specs": {"Material": {"class": "specs", "label": "Material"},
                              "Origin": {"class": "specs", "label": "Origin"}}}


def _spec_card(path: str, specs_html: str) -> str:
    return (f'<article class="starel-loop-card"><a class="card-link" href="{path}">x</a>'
            f'<h3 class="card-title">Stone Slab</h3>'
            f'<div class="starel-loop-card-price__row--sqm">£300.00 per m²</div>{specs_html}</article>')


@pytest.mark.parametrize("specs_html, expected", [
    # a th/td row
    ('<table class="specs"><tr><th>Material</th><td>Marble</td></tr><tr><th>Origin</th><td>Italy</td></tr></table>',
     {"Material": "Marble", "Origin": "Italy"}),
    # a dt/dd pair, the label's colon and case aside
    ('<dl class="specs"><dt>MATERIAL:</dt><dd>Granite</dd><dt>Finish</dt><dd>Polished</dd></dl>',
     {"Material": "Granite"}),
    # a label element followed by its text
    ('<div class="specs"><p><strong>Material :</strong> Quartzite</p><p><strong>Origin:</strong>  Brazil </p></div>',
     {"Material": "Quartzite", "Origin": "Brazil"}),
])
def test_a_labelled_source_reads_the_value_the_page_pairs_with_its_label(specs_html, expected):
    row = _rows(_run({f"{BASE}/collections/slabs/": _page(_spec_card("/p/s/", specs_html))}, _site(LABELLED)))["/p/s/"]
    assert row.stated_specs == expected


def test_a_label_matches_whole_so_another_label_is_never_read_and_the_spec_stays_empty():
    specs_html = '<table class="specs"><tr><th>Material type</th><td>Natural stone</td></tr></table>'
    row = _rows(_run({f"{BASE}/collections/slabs/": _page(_spec_card("/p/t/", specs_html))}, _site(LABELLED)))["/p/t/"]
    assert row.stated_specs == {}


# --- refusals: one record's problem, never the site's ----------------------------------------------------------------
def test_a_record_without_its_key_or_with_unparsable_json_is_refused_with_its_reason():
    no_key = ('<div class="items-loop__item" data-ms-tracking-item="' + _attr({"item_name": "No id slab"}) + '">'
              '<div class="items-loop__item-price">$ 10.00</div></div>')
    broken = '<div class="items-loop__item" data-ms-tracking-item="{not json"><div>x</div></div>'
    page = _page(_card("SL1", "Negro Marquina Polished Marble Slab Random", PRICED) + no_key + broken)
    result = _run({f"{BASE}/collections/slabs/": page}, _site(CARDS))
    assert sorted(_rows(result)) == ["SL1"]
    status = _status(result)
    assert (status.status.value, status.malformed_listings) == ("partial", 2)
    assert "listing_key not found in the record x1" in status.detail
    assert "listing_text JSON in data-ms-tracking-item does not parse x1" in status.detail


def test_a_required_field_whose_pattern_does_not_match_refuses_the_record():
    declaration = {**SALE, "fields": {**SALE["fields"], "listing_text": {"class": "card-title", "text": True,
                                                                         "pattern": r"^(\S+) Slab$"}}}
    card = ('<article class="starel-loop-card"><a class="card-link" href="/p/x/">x</a>'
            '<h3 class="card-title">Arabescato Marble Tile</h3></article>')
    status = _status(_run({f"{BASE}/collections/slabs/": _page(card)}, _site(declaration)))
    assert "listing_text pattern does not match 'Arabescato Marble Tile' x1" in status.detail


# --- the register row's own rules ------------------------------------------------------------------------------------
def test_a_declared_html_site_loads_with_its_declaration():
    site = _register(_site(CARDS)).sites[0]
    assert (site.html.read, site.html.record.class_) == ("listing_pages", "items-loop__item")


@pytest.mark.parametrize("change, message", [
    ({"html": None}, "html"),                                                           # approved needs it
    ({"html": {**CARDS, "record": {}}}, "record"),                                       # no selector
    ({"html": {**CARDS, "record": {"class": "a", "attribute": "data-x"}}}, "record"),    # two selectors
    ({"html": {**CARDS, "fields": {**CARDS["fields"], "listing_key": {"attr": "href", "text": True}}}}, "one of"),
    ({"html": {**CARDS, "fields": {**CARDS["fields"], "listing_key": {"text": True, "pattern": "no group"}}}},
     "one capture group"),
    ({"html": {**CARDS, "fields": {**CARDS["fields"], "listing_key": {"text": True, "pattern": "(a)(b)"}}}},
     "one capture group"),
    ({"html": {**CARDS, "specs": {"Regular price": {"json": ["data-x", "price"], "struck": True}}}}, "struck"),
    ({"html": {**CARDS, "fields": {**CARDS["fields"], "price": {"class": "p", "text": True, "struck": True}}}},
     "struck"),                                                          # struck text is never the price
    ({"html": {**CARDS, "specs": {"Material": {"class": "specs", "label": "Material", "text": True}}}}, "one of"),
    ({"discovery": {"sitemaps": ["/sitemap.xml"], "product_path": "^/p/"}}, "listing_pages"),  # read: listing_pages
])
def test_a_declaration_that_is_not_whole_is_refused(change, message):
    with pytest.raises(ValidationError, match=message):
        _register(_site(CARDS, **{k: v for k, v in change.items() if k != "discovery"},
                        **({"discovery": change["discovery"]} if "discovery" in change else {})))


def test_another_platform_takes_no_html_declaration():
    woo = {**_site(CARDS), "platform": "woocommerce_store", "discovery": None}
    with pytest.raises(ValidationError, match="takes no html"):
        _register(woo)


# --- the shipped blokport rows read a price whole or not at all (reviewer 2 on SC #785) --------------------------------
def _shipped(site_id: str):
    register = load_register("blokport")
    return register.model_copy(update={"sites": tuple(s for s in register.sites if s.site_id == site_id)})


def _amounts(register, answers: dict) -> dict:
    site = register.sites[0]
    transport = _Sites({f"{site.base_url}/robots.txt": ROBOTS_OPEN, **answers})
    result = collect_run(register, transport, MOMENT, lambda _seconds: None)
    return {row.listing_text: (None if row.price_amount is None else Decimal(str(row.price_amount)))
            for row in result.observations}


def test_the_shipped_stoneks_row_reads_a_price_whole_or_leaves_it_unpriced_never_a_part_of_it():
    register = _shipped("stoneks")
    base = register.sites[0].base_url
    shown = {"Alpha": "313", "Beta": "120,50", "Gamma": "1 234"}     # whole euros; a decimal comma; a grouped figure
    links = "".join(f'<a href="/en/product/{name.lower()}/">{name}</a>' for name in shown)
    answers = {f"{base}{path}": _page(links if path.endswith("/granite/") else "")
               for path in register.sites[0].discovery.listing_pages}
    for index, (name, amount) in enumerate(shown.items()):
        answers[f"{base}/en/product/{name.lower()}/"] = _page(
            f'<div id="post-{index}" class="product type-product"><h1 class="fusion-title-heading">{name}</h1>'
            f'<p class="price"><span class="woocommerce-Price-amount amount"><bdi>{amount}&nbsp;&euro;</bdi></span>'
            f'<span class="stx-unit">/m²</span></p><table class="woocommerce-product-attributes"><tr><th>Material</th>'
            f'<td><p>Granite</p></td></tr></table></div>')
    # its declared notation has no decimal comma and no grouping: those forms are unpriced, never read as 50 or 234
    assert _amounts(register, answers) == {"Alpha": Decimal("313"), "Beta": None, "Gamma": None}


def test_the_shipped_terra_row_reads_a_grouped_price_with_its_pence_whole():
    register = _shipped("mosstonex_terra")
    base = register.sites[0].base_url
    shown = {"Carrara Marble": "185", "Calacatta Viola Marble": "1,234.50"}
    links = "".join(f'<a href="/product/{index}/">{name}</a>' for index, name in enumerate(shown))
    answers = {f"{base}/Terra/": _page(links)}
    for index, (name, amount) in enumerate(shown.items()):
        answers[f"{base}/product/{index}/"] = _page(
            f'<div class="elementor-location-single product type-product"><h1 class="elementor-heading-title">{name}'
            f'</h1><p class="elementor-heading-title"><span class="woocommerce-Price-amount amount"><span '
            f'class="woocommerce-Price-currencySymbol">&pound;</span>{amount}</span></p></div>')
    assert _amounts(register, answers) == {"Carrara Marble": Decimal("185"),
                                           "Calacatta Viola Marble": Decimal("1234.50")}
