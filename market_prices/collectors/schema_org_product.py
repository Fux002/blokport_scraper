"""A retail site's product pages, read from their schema.org Product/Offer structured data (JSON-LD), reached through
the entry points the site's register row names (its sitemaps or listing pages). Any shop that states its products this
way is config only: no per-site code.

What a page does not state in its structured data is not read from its HTML. A site whose prices are loaded by script,
or need a browser, gives no rows and says why in site_status, never worked around. Every scan is linear: an opening tag,
then the next closing tag after it, resuming where the last stopped, so a hostile page cannot stall the run.
"""

from __future__ import annotations

import html
import json
import re
import zlib
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Iterator, Mapping, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from market_prices import config
from market_prices.fetching import Response, SiteFetcher, UnexpectedResponse, UnexpectedStatus
from market_prices.schema import ListingFacts, MalformedListing, PriceStatus, VatBasis
from market_prices.text import clean_text, on_site, site_host

if TYPE_CHECKING:
    from market_prices.register import Site

_PRODUCT_TYPES = frozenset({"Product", "IndividualProduct", "ProductModel"})
_GROUP_TYPE = "ProductGroup"
_AGGREGATE_TYPE = "AggregateOffer"
# [^<>]: a tag ends at the next '<' as well, so an unclosed one fails at once instead of scanning the rest of the page
_SCRIPT_OPEN = re.compile(r"<script\b[^<>]*>", re.IGNORECASE)
_SCRIPT_CLOSE = re.compile(r"</script\s*>", re.IGNORECASE)
_JSON_LD = re.compile(r"""\btype\s*=\s*["']?\s*application/ld\+json\b""", re.IGNORECASE)
_LOC_OPEN = re.compile(r"<loc\s*>", re.IGNORECASE)
_LOC_CLOSE = re.compile(r"</loc\s*>", re.IGNORECASE)
_SITEMAP_INDEX = re.compile(r"<sitemapindex\b", re.IGNORECASE)
_LINK_TAG = re.compile(r"<(?:a|link)\b[^<>]*>", re.IGNORECASE)
_HREF = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
_REL_NEXT = re.compile(r"""\brel\s*=\s*(?:"[^"]*\bnext\b[^"]*"|'[^']*\bnext\b[^']*'|next\b)""", re.IGNORECASE)
_DECIMAL = re.compile(r"\d+(\.\d+)?")
_GZIP_MAGIC = b"\x1f\x8b"
_SEVERAL_CURRENCIES = "offers state several currencies"
_VARIANT_ID_FIELDS = ("@id", "url")
_SPEC_FIELDS = ("brand", "gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn", "sku", "productID", "color", "material",
                "size", "width", "height", "depth", "weight")
_KEY_FIELDS = ("sku", "productID")


@dataclass(frozen=True)
class _Listing:
    page_url: str
    node: Mapping
    group: Optional[Mapping]


@dataclass(frozen=True)
class _Skipped:
    """A product page that gives no listing, and why: counted against the site, the other pages still count."""

    reason: str


def _between(text: str, opening: re.Pattern, closing: re.Pattern) -> Iterator[tuple[str, str]]:
    """Each (opening tag, text up to the next closing tag), in page order. Once no closing tag follows an opening, no
    later item can close either, so the scan stops."""
    at = 0
    while (opened := opening.search(text, at)) is not None:
        closed = closing.search(text, opened.end())
        if closed is None:
            return
        yield opened.group(0), text[opened.end():closed.start()]
        at = closed.end()


def _normal(url: str) -> str:
    """The URL without its fragment: the same page however it was linked."""
    return urlunsplit(urlsplit(url)._replace(fragment=""))


def _types(node: Mapping) -> set[str]:
    """The node's schema.org types, a full IRI or a bare name alike."""
    declared = node.get("@type")
    names = declared if isinstance(declared, list) else [declared]
    return {name.rsplit("/", 1)[-1] for name in names if isinstance(name, str)}


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else [value] if value is not None else []


def _text(value: object) -> Optional[str]:
    """A stated scalar as text. Numbers arrive as their exact JSON text (parse_float/parse_int=str)."""
    return clean_text(value) if isinstance(value, str) else None


# --- discovery -------------------------------------------------------------------------------------------------------
def _sitemap_text(response: Response, url: str) -> str:
    """The sitemap's text; a gzipped file (served as a .gz, not as a transfer encoding) is unzipped under a cap."""
    if response.body[:2] != _GZIP_MAGIC:
        return response.text
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(response.body, config.SCHEMA_ORG_MAX_UNZIPPED_BYTES + 1)
    except zlib.error as exc:
        raise UnexpectedResponse(f"{url} is not a readable gzip file") from exc
    if len(data) > config.SCHEMA_ORG_MAX_UNZIPPED_BYTES:
        raise UnexpectedResponse(f"{url} unzipped is over the cap of {config.SCHEMA_ORG_MAX_UNZIPPED_BYTES} bytes")
    if inflater.unused_data:    # zlib reads one member; a second would otherwise be left unread, silently
        raise UnexpectedResponse(f"{url} holds more than one gzip member")
    return data.decode("utf-8", errors="replace")


def _sitemap_pages(site: "Site", fetcher: SiteFetcher) -> set[str]:
    """Every page the entry sitemaps list. An index's sitemaps on the site are followed, breadth first, under a count
    and a depth cap; one off the site is never requested."""
    pages: set[str] = set()
    queue = [(f"{site.base_url}{path}", 0) for path in site.discovery.sitemaps]
    seen: set[str] = set()
    while queue:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if len(seen) > config.SCHEMA_ORG_MAX_SITEMAPS:
            raise UnexpectedResponse(f"the entry points list over {config.SCHEMA_ORG_MAX_SITEMAPS} sitemaps")
        text = _sitemap_text(fetcher.get(url), url)
        locs = [_normal(urljoin(url, html.unescape(loc.strip()))) for _, loc in _between(text, _LOC_OPEN, _LOC_CLOSE)]
        if not _SITEMAP_INDEX.search(text):
            pages.update(locs)
            continue
        if depth >= config.SCHEMA_ORG_MAX_SITEMAP_DEPTH:
            raise UnexpectedResponse(f"sitemap index nested deeper than {config.SCHEMA_ORG_MAX_SITEMAP_DEPTH} at {url}")
        queue.extend((loc, depth + 1) for loc in locs if on_site(loc, site.base_url))
    return pages


def _href(tag: str) -> Optional[str]:
    found = _HREF.search(tag)
    return next((group for group in found.groups() if group is not None), None) if found else None


def _listing_links(site: "Site", fetcher: SiteFetcher) -> set[str]:
    """Every link on the entry listing pages and the pages their rel="next" links lead to, under a page cap. A next
    link off the site, or back to a page already read, ends that entry's pages."""
    links: set[str] = set()
    seen: set[str] = set()
    for path in site.discovery.listing_pages:
        url: Optional[str] = f"{site.base_url}{path}"
        while url is not None and url not in seen:
            seen.add(url)
            if len(seen) > config.SCHEMA_ORG_MAX_LISTING_PAGES:
                raise UnexpectedResponse(f"the entry points lead to over {config.SCHEMA_ORG_MAX_LISTING_PAGES} "
                                         f"listing pages")
            following = None
            for tag in _LINK_TAG.findall(fetcher.get(url).text):
                href = _href(tag)
                if href is None:
                    continue
                target = _normal(urljoin(url, html.unescape(href)))
                if _REL_NEXT.search(tag):
                    following = following or target
                else:
                    links.add(target)
            url = following if following is not None and on_site(following, site.base_url) else None
    return links


def _product_urls(site: "Site", fetcher: SiteFetcher) -> list[str]:
    """The product pages the entry points lead to: on the site, the path matching product_path, sorted."""
    pattern = re.compile(site.discovery.product_path)
    found = _sitemap_pages(site, fetcher) | _listing_links(site, fetcher)
    return sorted(url for url in found if on_site(url, site.base_url) and pattern.search(urlsplit(url).path))


# --- the structured data ---------------------------------------------------------------------------------------------
def _no_constant(name: str) -> None:
    """NaN and Infinity are not prices or ids: read as null."""
    return None


def _blocks(page: str) -> list:
    """Every JSON-LD block that parses. A broken block (or hostile nesting) is that block's problem alone."""
    blocks = []
    for tag, body in _between(page, _SCRIPT_OPEN, _SCRIPT_CLOSE):
        if not _JSON_LD.search(tag):
            continue
        try:
            blocks.append(json.loads(body, parse_float=str, parse_int=str, parse_constant=_no_constant))
        except (ValueError, RecursionError):
            continue
    return blocks


def _collect(value: object, found: list, depth: int) -> None:
    """Every Product or ProductGroup node, not descending into one (a group's variants are read from the group)."""
    if depth > config.SCHEMA_ORG_MAX_NESTING:
        return
    if isinstance(value, list):
        for item in value:
            _collect(item, found, depth + 1)
    elif isinstance(value, Mapping):
        if _types(value) & (_PRODUCT_TYPES | {_GROUP_TYPE}):
            found.append(value)
            return
        for item in value.values():
            _collect(item, found, depth + 1)


def _same_page(stated: object, page_url: str) -> bool:
    if not isinstance(stated, str):
        return False
    seen, page = urlsplit(urljoin(page_url, stated)), urlsplit(page_url)
    return (site_host(seen.geturl()) == site_host(page.geturl())
            and seen.path.rstrip("/") == page.path.rstrip("/"))


def _page_products(page: str, page_url: str) -> list[tuple[Mapping, Optional[Mapping]]]:
    """The page's own product and, for a ProductGroup, each of its variants with the group."""
    found: list = []
    for block in _blocks(page):
        _collect(block, found, 0)
    if not found:
        raise MalformedListing("no schema.org Product on the page")
    if len(found) > 1:
        own = [node for node in found if _same_page(node.get("url"), page_url) or _same_page(node.get("@id"), page_url)]
        if len(own) != 1:
            raise MalformedListing("several schema.org Products and none is the page's own")
        found = own
    node = found[0]
    if _GROUP_TYPE in _types(node) and "hasVariant" in node:
        variants = [item for item in _as_list(node.get("hasVariant"))
                    if isinstance(item, Mapping) and _types(item) & _PRODUCT_TYPES]
        if not variants:
            raise MalformedListing("ProductGroup lists no readable variant")
        return [(variant, node) for variant in variants]
    return [(node, None)]


def _offers(node: Mapping) -> list[Mapping]:
    return [offer for offer in _as_list(node.get("offers")) if isinstance(offer, Mapping)]


def _currency(value: object) -> Optional[str]:
    code = _text(value)
    return code if code and len(code) == 3 and code.isascii() and code.isalpha() and code.isupper() else None


def _offer_prices(offer: Mapping) -> list[tuple[str, Optional[str]]]:
    """(price as stated, currency) for each price the offer states: an aggregate's bounds, else its price, else its
    price specifications'."""
    currency = _currency(offer.get("priceCurrency"))
    if _AGGREGATE_TYPE in _types(offer):
        bounds = [_text(offer.get(name)) for name in ("lowPrice", "highPrice")]
        if any(bounds):
            return [(bound, currency) for bound in bounds if bound]
    price = _text(offer.get("price"))
    if price:
        return [(price, currency)]
    return [(spec_price, _currency(spec.get("priceCurrency")) or currency)
            for spec in _as_list(offer.get("priceSpecification")) if isinstance(spec, Mapping)
            for spec_price in [_text(spec.get("price"))] if spec_price]


def _exact(price: str) -> Optional[str]:
    """One exact positive decimal in schema.org's form (a dot, no grouping); anything else is not a price here."""
    return price if _DECIMAL.fullmatch(price) and Decimal(price) > 0 else None


def _price(offers: list[Mapping]) -> tuple[Optional[str], Optional[str], Optional[str], tuple[str, ...]]:
    """(price_amount, price_text, currency, notes). One stated price is one price; several are a span, and prices in
    several currencies are not one price either: both kept as text, never one chosen."""
    stated = [pair for offer in offers for pair in _offer_prices(offer)]
    if not stated:
        return None, None, None, ()
    currencies = {currency for _, currency in stated if currency}
    if len(currencies) > 1:
        text = " | ".join(sorted({f"{price} {currency}" if currency else price for price, currency in stated}))
        return None, text, None, (_SEVERAL_CURRENCIES,)
    currency = next(iter(currencies), None)
    distinct = sorted({price for price, _ in stated},
                      key=lambda price: (0, Decimal(price), price) if _DECIMAL.fullmatch(price) else (1, 0, price))
    shown = distinct[0] if len(distinct) == 1 else f"{distinct[0]}-{distinct[-1]}"
    text = f"{shown} {currency}" if currency else shown
    return (_exact(distinct[0]) if len(distinct) == 1 else None), text, currency, ()


def _vat(offers: list[Mapping]) -> Optional[VatBasis]:
    """The offers' own statement of VAT: one agreed value, else none."""
    said = set()
    for offer in offers:
        for holder in [offer, *(spec for spec in _as_list(offer.get("priceSpecification")) if isinstance(spec, Mapping))]:
            value = holder.get("valueAddedTaxIncluded")
            if isinstance(value, str):
                value = {"true": True, "false": False}.get(value.strip().lower())
            if isinstance(value, bool):
                said.add(VatBasis.incl if value else VatBasis.excl)
    return next(iter(said)) if len(said) == 1 else None


def _availability(offers: list[Mapping]) -> Optional[str]:
    stated = sorted({text.rsplit("/", 1)[-1] for offer in offers for text in [_text(offer.get("availability"))] if text})
    return " / ".join(stated) or None


def _min_qty(offers: list[Mapping]) -> Optional[str]:
    """A stated minimum above one; the offers must agree."""
    stated = {_text(quantity.get("minValue")) for offer in offers
              for quantity in [offer.get("eligibleQuantity")] if isinstance(quantity, Mapping)}
    stated.discard(None)
    if len(stated) != 1:
        return None
    minimum = next(iter(stated))
    return minimum if minimum.isdigit() and int(minimum) > 1 else None


def _spec_value(value: object) -> Optional[str]:
    """A stated value as written: text, a named thing (a Brand), or a quantity with its unit."""
    if isinstance(value, list):
        parts = [part for part in (_spec_value(item) for item in value) if part]
        return " / ".join(parts) or None
    if isinstance(value, Mapping):
        if "value" in value:
            number = _text(value.get("value"))
            unit = _text(value.get("unitText")) or _text(value.get("unitCode"))
            return f"{number} {unit}" if number and unit else number
        return _text(value.get("name"))
    return _text(value)


def _specs(node: Mapping) -> dict[str, str]:
    """The product's stated fields, then each additionalProperty; a label is kept once, compared without case."""
    specs: dict[str, str] = {}
    for name in _SPEC_FIELDS:
        value = _spec_value(node.get(name))
        if value:
            specs[name] = value
    taken = {label.casefold() for label in specs}
    for prop in _as_list(node.get("additionalProperty")):
        if not isinstance(prop, Mapping):
            continue
        label, value = _text(prop.get("name")), _spec_value(prop)
        if label and value and label.casefold() not in taken:
            specs[label] = value
            taken.add(label.casefold())
    return specs


def _grade(specs: Mapping[str, str], labels: tuple[str, ...]) -> Optional[str]:
    wanted = {label.lower() for label in labels}
    return next((value for label, value in sorted(specs.items()) if label.lower() in wanted), None)


def _address_key(url: str) -> str:
    """A URL's path, query and fragment: the part that tells one listing of the site from another."""
    parts = urlsplit(url)
    return (parts.path + (f"?{parts.query}" if parts.query else "")
            + (f"#{parts.fragment}" if parts.fragment else ""))


def _key(node: Mapping, page_url: str, variant: bool) -> str:
    """The site's own id (sku, else productID). Without one, a product is keyed by its page; a variant shares its page
    with the others, so it is keyed by its own @id or url on the site, and with no identity of its own it is refused,
    never collapsed into another variant."""
    for name in _KEY_FIELDS:
        key = _text(node.get(name))
        if key:
            return key
    if not variant:
        return _address_key(page_url)
    page = _address_key(page_url)
    for name in _VARIANT_ID_FIELDS:
        stated = node.get(name)
        if isinstance(stated, str):
            absolute = urljoin(page_url, stated)
            if on_site(absolute, page_url) and _address_key(absolute) not in ("", page):
                return _address_key(absolute)
    raise MalformedListing("variant has no id of its own")


def _source_url(node: Mapping, page_url: str, base_url: str) -> str:
    stated = node.get("url")
    if isinstance(stated, str):
        absolute = _normal(urljoin(page_url, stated))
        if on_site(absolute, base_url):
            return absolute
    return page_url


class SchemaOrgProductCollector:
    platform = "schema_org_product"
    version = "schema_org_product-1"
    takes_query = False
    needs_discovery = True
    reserved_query: frozenset = frozenset()

    def listings(self, site: "Site", fetcher: SiteFetcher) -> Iterator[object]:
        """Each product page's listings, in sorted page order, up to the cap; reaching it ends the read as partial."""
        urls = _product_urls(site, fetcher)
        if not urls:
            raise UnexpectedResponse("the entry points list no product page matching product_path")
        cap = config.SCHEMA_ORG_MAX_PRODUCT_PAGES
        for url in urls[:cap]:
            yield from self._page(url, fetcher)
        if len(urls) > cap:
            raise UnexpectedResponse(f"the entry points list {len(urls)} product pages, over the cap of {cap}: "
                                     f"the first {cap} in sorted order were read")

    @staticmethod
    def _page(url: str, fetcher: SiteFetcher) -> Iterator[object]:
        """One product page. robots.txt is asked first, so a page it forbids is skipped unrequested; a page that
        answers but is not usable is its listing's problem. A block or no answer at all still ends the site."""
        if not fetcher.allows(url):
            yield _Skipped("robots.txt disallows the product page")
            return
        try:
            page = fetcher.get(url)
        except UnexpectedStatus as exc:
            yield _Skipped(f"product page HTTP {exc.status}")
            return
        except UnexpectedResponse:
            yield _Skipped("product page not usable")
            return
        try:
            products = _page_products(page.text, url)
        except MalformedListing as exc:
            yield _Skipped(str(exc))
            return
        for node, group in products:
            yield _Listing(url, node, group)

    def facts(self, raw: object, site: "Site") -> ListingFacts:
        if isinstance(raw, _Skipped):
            raise MalformedListing(raw.reason)
        if not isinstance(raw, _Listing):
            raise MalformedListing("not a product page listing")
        node, group = raw.node, raw.group
        name = _text(node.get("name")) or (_text(group.get("name")) if group is not None else None)
        if not name:
            raise MalformedListing("Product has no name")
        offers = _offers(node)
        amount, price_text, currency, notes = _price(offers)
        specs = _specs(node)
        return ListingFacts(
            listing_key=_key(node, raw.page_url, variant=group is not None),
            source_url=_source_url(node, raw.page_url, site.base_url),
            listing_text=name,
            stated_specs=specs,
            grade_text=_grade(specs, site.grade_labels),
            price_text=price_text,
            price_amount=amount,
            currency=currency,
            price_status=PriceStatus.priced if amount is not None else PriceStatus.none,
            min_qty_text=_min_qty(offers),
            availability_text=_availability(offers),
            vat_basis=_vat(offers),
            notes=notes,
        )
