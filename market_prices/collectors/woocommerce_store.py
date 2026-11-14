"""WooCommerce Store API (/wp-json/wc/store/v1/products): public, unauthenticated, the shop's own JSON."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Iterator, Mapping, Optional
from urllib.parse import urlencode

from market_prices import config
from market_prices.fetching import SiteFetcher, UnexpectedResponse
from market_prices.schema import ListingFacts, MalformedListing, PriceStatus
from market_prices.html_tree import Element, PageTooLarge, parse, text
from market_prices.text import clean_text, on_site

if TYPE_CHECKING:
    from market_prices.register import Site

_API_SPEC_FIELDS = ("sku", "weight", "formatted_weight", "formatted_dimensions")
_DIMENSION_FIELDS = ("length", "width", "height")
# [^<>]: an opening tag ends at the next '<' as well, so a '<li' that never reaches its '>' fails at once instead of
# scanning to the end of the page on every attempt (quadratic on a page of them)
_LIST_OPEN = re.compile(r"<li\b[^<>]*>", re.IGNORECASE)
_LIST_CLOSE = re.compile(r"</li\s*>", re.IGNORECASE)


def _mapping(value: object, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise MalformedListing(f"{what} is not an object")
    return value


def _sequence(value: object, what: str) -> list:
    if not isinstance(value, list):
        raise MalformedListing(f"{what} is not a list")
    return value


def _is_count(value: object) -> bool:
    return (isinstance(value, str) and value.isascii() and value.isdigit()
            and len(value) <= config.WOO_MAX_DIGITS)


def _amount(prices: Mapping) -> Optional[str]:
    """The price as an exact decimal string, or None when the listing has no single price. The API gives integer
    minor units as text. Zero marks a made-to-measure configurator, and a differing range is a variable product's
    span: neither is a price, and the seller's own wording stays in price_text."""
    price_range = prices.get("price_range")
    if price_range and _mapping(price_range, "price_range").get("min_amount") != price_range.get("max_amount"):
        return None
    raw = prices.get("price")
    if raw in (None, ""):
        return None
    minor = prices.get("currency_minor_unit")
    if not _is_count(raw):
        raise MalformedListing("price is not integer minor units")
    if not isinstance(minor, int) or isinstance(minor, bool) or not 0 <= minor <= config.WOO_MAX_MINOR_UNIT:
        raise MalformedListing(f"currency_minor_unit {minor!r} is not usable")
    value = int(raw)
    if value == 0:
        return None
    whole, fraction = divmod(value, 10 ** minor)
    return f"{whole}.{fraction:0{minor}d}" if minor else str(whole)


def _shown(price_html: object) -> Optional[Element]:
    """The listing's price_html as a parsed page fragment; one over the element cap is this listing's problem."""
    if not isinstance(price_html, str):
        return None
    try:
        return parse(price_html)
    except PageTooLarge as exc:
        raise MalformedListing(f"price_html: {exc}") from exc


def _regular_amount(prices: Mapping, amount: Optional[str]) -> Optional[str]:
    """The Store API's own regular_price, when the listing has one price and its regular price differs (it is on
    sale); read as the price is (integer minor units)."""
    if amount is None or prices.get("regular_price") in (None, ""):
        return None
    regular = _amount({**prices, "price": prices.get("regular_price"), "price_range": None})
    return regular if regular is not None and regular != amount else None


def _regular_text(shown: Optional[Element], regular_amount: str, currency: Optional[str]) -> str:
    """The regular price as the shop writes it: the struck "was" text of its price_html, or, where the html shows
    none, the API's own statement of it (amount and code)."""
    struck = text(shown, struck=True, hidden=config.WOOCOMMERCE_HIDDEN_CLASSES) if shown is not None else None
    return struck or " ".join(part for part in (regular_amount, currency) if part)


def _listing_key(value: object) -> str:
    """An integer or non-blank text id. A float would not be stable between runs."""
    key = clean_text(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else None
    if not key:
        raise MalformedListing("listing has no usable id")
    return key


def _currency(prices: Mapping) -> Optional[str]:
    code = clean_text(prices.get("currency_code"))
    return code if code and len(code) == 3 and code.isascii() and code.isalpha() and code.isupper() else None


def _attributes(listing: Mapping) -> dict[str, str]:
    """Each attribute's label and its values, as the seller wrote them."""
    labelled: dict[str, str] = {}
    for attribute in _sequence(listing.get("attributes") or [], "attributes"):
        attribute = _mapping(attribute, "attribute")
        label = clean_text(attribute.get("name"))
        terms = _sequence(attribute.get("terms") or [], "terms")
        values = [clean_text(_mapping(term, "term").get("name")) for term in terms]
        kept = " / ".join(value for value in values if value)
        if label and kept:
            labelled[label] = f"{labelled[label]} / {kept}" if label in labelled else kept
    return labelled


def _list_items(html: str) -> list[str]:
    """The text inside each <li>...</li>, in page order: an opening tag, then the next closing tag after it. Each
    search resumes where the last one stopped, so the scan is linear even on a hostile page of unclosed <li> (a
    lazy regex over the whole item retried from every opening, and a megabyte of them stalled a listing for minutes).
    Once no closing tag follows an opening, no later item can close either, so the scan stops."""
    items, at = [], 0
    while (opening := _LIST_OPEN.search(html, at)) is not None:
        closing = _LIST_CLOSE.search(html, opening.end())
        if closing is None:
            break
        items.append(html[opening.end():closing.start()])
        at = closing.end()
    return items


def _description_specs(listing: Mapping) -> list[tuple[str, str]]:
    """The "Label: value" items of the description's HTML list, as the seller wrote them, in page order. An item with
    no label, an empty side, or a label or value too long to be a spec is prose and is left out."""
    description = listing.get("description")
    if not isinstance(description, str):
        return []
    items = []
    for item in _list_items(description):
        text = clean_text(item)
        if not text or ":" not in text:
            continue
        label, value = (part.strip() for part in text.split(":", 1))
        if (label and value and len(label) <= config.DESCRIPTION_SPEC_MAX_LABEL_CHARS
                and len(value) <= config.DESCRIPTION_SPEC_MAX_VALUE_CHARS):
            items.append((label, value))
    return items


def _stated_specs(listing: Mapping, attributes: Mapping[str, str]) -> dict[str, str]:
    """Every spec the listing states: its attributes, then the API's own fields, then the description's list items. A
    description item never replaces a label already stated, compared without case ("dikte" is the attribute Dikte)."""
    specs = dict(attributes)
    dimensions = listing.get("dimensions") or {}
    sources = {name: listing.get(name) for name in _API_SPEC_FIELDS}
    sources.update({name: _mapping(dimensions, "dimensions").get(name) for name in _DIMENSION_FIELDS})
    for name, value in sources.items():
        text = clean_text(value)
        if text:
            specs.setdefault(name, text)
    taken = {label.casefold() for label in specs}
    for label, value in _description_specs(listing):
        if label.casefold() not in taken:
            specs[label] = value
            taken.add(label.casefold())
    return specs


def _grade(attributes: Mapping[str, str], labels: tuple[str, ...]) -> Optional[str]:
    wanted = {label.lower() for label in labels}
    return next((value for label, value in sorted(attributes.items()) if label.lower() in wanted), None)


def _min_qty(listing: Mapping) -> Optional[str]:
    """Only a minimum above one is the seller's statement: the API reports 1 for every listing that sets none."""
    minimum = _mapping(listing.get("add_to_cart") or {}, "add_to_cart").get("minimum")
    return str(minimum) if isinstance(minimum, int) and not isinstance(minimum, bool) and minimum > 1 else None


class WooCommerceStoreCollector:
    platform = "woocommerce_store"
    version = "woocommerce_store-1"
    takes_query = True
    needs_discovery = False
    needs_html = False
    needs_scrape = False
    fetches = True
    reserved_query = frozenset({"page", "per_page"})

    def _page_url(self, site: "Site", page: int) -> str:
        query = {**site.query, "per_page": config.WOO_PAGE_SIZE, "page": page}
        return f"{site.base_url}{config.WOO_STORE_PATH}?{urlencode(sorted(query.items()))}"

    def listings(self, site: "Site", fetcher: SiteFetcher) -> Iterator[object]:
        """The first page's total stands for the run, so a site that lowers it midway cannot end a short read as ok."""
        first_url = self._page_url(site, 1)
        first = fetcher.get(first_url)
        total = self._total_pages(first.header(config.WOO_TOTAL_PAGES_HEADER), first_url)
        yield from self._items(first.text, first_url)
        for page in range(2, total + 1):
            url = self._page_url(site, page)
            yield from self._items(fetcher.get(url).text, url)

    @staticmethod
    def _total_pages(header: str, url: str) -> int:
        if not _is_count(header):
            raise UnexpectedResponse(f"no usable {config.WOO_TOTAL_PAGES_HEADER} from {url}")
        total = int(header)
        if total > config.WOO_MAX_PAGES:
            raise UnexpectedResponse(f"{url} advertises {total} pages, over the cap of {config.WOO_MAX_PAGES}")
        return total

    @staticmethod
    def _items(body: str, url: str) -> list:
        try:
            items = json.loads(body)
        except (ValueError, RecursionError) as exc:     # JSONDecodeError, an over-long integer, or hostile nesting
            raise UnexpectedResponse(f"not usable JSON from {url}") from exc
        if not isinstance(items, list):
            raise UnexpectedResponse(f"not a product list from {url}")
        return items

    def listing_text(self, raw: object, site: "Site") -> str:
        """The seller's name for the listing and its short description: the text the run's scope is decided on,
        before anything else of the listing is read."""
        listing = _mapping(raw, "listing")
        name = clean_text(listing.get("name"))
        if not name:
            raise MalformedListing("listing has no name")
        summary = clean_text(listing.get("short_description"))
        return f"{name} | {summary}" if summary else name

    def facts(self, raw: object, site: "Site") -> ListingFacts:
        listing_text = self.listing_text(raw, site)
        listing = _mapping(raw, "listing")
        key = _listing_key(listing.get("id"))
        url = listing.get("permalink")
        if not isinstance(url, str) or not on_site(url, site.base_url):
            raise MalformedListing(f"permalink is not on {site.base_url}")
        prices = _mapping(listing.get("prices"), "prices")
        amount = _amount(prices)
        shown = _shown(listing.get("price_html"))
        regular_amount = _regular_amount(prices, amount)
        currency = _currency(prices)
        attributes = _attributes(listing)
        stock = listing.get("stock_availability") or {}
        return ListingFacts(
            listing_key=key,
            source_url=url,
            listing_text=listing_text,
            stated_specs=_stated_specs(listing, attributes),
            grade_text=_grade(attributes, site.grade_labels),
            price_text=text(shown, hidden=config.WOOCOMMERCE_HIDDEN_CLASSES) if shown is not None else None,
            price_amount=amount,
            regular_price_text=(_regular_text(shown, regular_amount, currency) if regular_amount is not None
                                else None),
            regular_price_amount=regular_amount,
            currency=currency,
            price_status=PriceStatus.priced if amount is not None else PriceStatus.none,
            min_qty_text=_min_qty(listing),
            availability_text=clean_text(_mapping(stock, "stock_availability").get("text")),
        )
