"""WooCommerce Store API (/wp-json/wc/store/v1/products): public, unauthenticated, the shop's own JSON."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Iterator, Mapping, Optional
from urllib.parse import urlencode

from market_prices import config
from market_prices.fetching import SiteFetcher, UnexpectedResponse
from market_prices.schema import ListingFacts, MalformedListing, PriceStatus
from market_prices.text import clean_text, on_site

if TYPE_CHECKING:
    from market_prices.register import Site

_API_SPEC_FIELDS = ("sku", "weight", "formatted_weight", "formatted_dimensions")
_DIMENSION_FIELDS = ("length", "width", "height")


def _mapping(value: object, what: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise MalformedListing(f"{what} is not an object")
    return value


def _sequence(value: object, what: str) -> list:
    if not isinstance(value, list):
        raise MalformedListing(f"{what} is not a list")
    return value


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
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdigit():
        raise MalformedListing(f"price {raw!r} is not integer minor units")
    if not isinstance(minor, int) or isinstance(minor, bool) or not 0 <= minor <= config.WOO_MAX_MINOR_UNIT:
        raise MalformedListing(f"currency_minor_unit {minor!r} is not usable")
    value = int(raw)
    if value == 0:
        return None
    whole, fraction = divmod(value, 10 ** minor)
    return f"{whole}.{fraction:0{minor}d}" if minor else str(whole)


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
            labelled[label] = kept
    return labelled


def _stated_specs(listing: Mapping, attributes: Mapping[str, str]) -> dict[str, str]:
    specs = dict(attributes)
    dimensions = listing.get("dimensions") or {}
    sources = {name: listing.get(name) for name in _API_SPEC_FIELDS}
    sources.update({name: _mapping(dimensions, "dimensions").get(name) for name in _DIMENSION_FIELDS})
    for name, value in sources.items():
        text = clean_text(value)
        if text:
            specs.setdefault(name, text)
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
    reserved_query = frozenset({"page", "per_page"})

    def _page_url(self, site: "Site", page: int) -> str:
        query = {**site.query, "per_page": config.WOO_PAGE_SIZE, "page": page}
        return f"{site.base_url}{config.WOO_STORE_PATH}?{urlencode(sorted(query.items()))}"

    def listings(self, site: "Site", fetcher: SiteFetcher) -> Iterator[object]:
        page, total = 1, 1
        while page <= total:
            url = self._page_url(site, page)
            response = fetcher.get(url)
            total = self._total_pages(response.header(config.WOO_TOTAL_PAGES_HEADER), url)
            yield from self._items(response.text, url)
            page += 1

    @staticmethod
    def _total_pages(header: str, url: str) -> int:
        if not header.isascii() or not header.isdigit():
            raise UnexpectedResponse(f"no usable {config.WOO_TOTAL_PAGES_HEADER} from {url}: {header!r}")
        total = int(header)
        if total > config.WOO_MAX_PAGES:
            raise UnexpectedResponse(f"{url} advertises {total} pages, over the cap of {config.WOO_MAX_PAGES}")
        return total

    @staticmethod
    def _items(body: str, url: str) -> list:
        try:
            items = json.loads(body)
        except json.JSONDecodeError as exc:
            raise UnexpectedResponse(f"not JSON from {url}: {exc}") from exc
        if not isinstance(items, list):
            raise UnexpectedResponse(f"not a product list from {url}")
        return items

    def facts(self, raw: object, site: "Site") -> ListingFacts:
        listing = _mapping(raw, "listing")
        key, name = clean_text(listing.get("id")), clean_text(listing.get("name"))
        url = listing.get("permalink")
        if not key or not name:
            raise MalformedListing("listing has no id or name")
        if not isinstance(url, str) or not on_site(url, site.base_url):
            raise MalformedListing(f"permalink {url!r} is not on {site.base_url}")
        prices = _mapping(listing.get("prices"), "prices")
        amount = _amount(prices)
        attributes = _attributes(listing)
        stock = listing.get("stock_availability") or {}
        summary = clean_text(listing.get("short_description"))
        return ListingFacts(
            listing_key=key,
            source_url=url,
            listing_text=f"{name} | {summary}" if summary else name,
            stated_specs=_stated_specs(listing, attributes),
            grade_text=_grade(attributes, site.grade_labels),
            price_text=clean_text(listing.get("price_html")),
            price_amount=amount,
            currency=_currency(prices),
            price_status=PriceStatus.priced if amount is not None else PriceStatus.none,
            min_qty_text=_min_qty(listing),
            availability_text=clean_text(_mapping(stock, "stock_availability").get("text")),
        )
