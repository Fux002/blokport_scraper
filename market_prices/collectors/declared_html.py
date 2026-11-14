"""A site whose listings are read where its register row DECLARES them (platform declared_html):
the row's `html` names the pages to read, what one listing is on them (a record) and where each fact sits in a record
(market_prices/html_declaration.py). One logic for every such site; a new one is config only, never per-site code.

What a page shows is what is read. A price is the price element's text as shown; its amount is read in the site's
declared number notation, and a card that shows no price is unpriced with its words kept, whatever a script or an
analytics attribute holds. Struck text is never the price: the sale rule reads it, where the row declares it, as
the regular price (contract v4). A required field the record does not hold is that listing's problem, counted with
its reason, never the site's; a spec not stated in its declared form is simply not stated."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator, Optional
from urllib.parse import urljoin

from market_prices import config
from market_prices.discovery import listing_pages, normal, product_urls
from market_prices.fetching import SiteFetcher, UnexpectedResponse, UnexpectedStatus
from market_prices.html_declaration import HtmlDeclaration, HtmlSource
from market_prices.html_tree import (Element, PageTooLarge, collapse, descendants, first, labelled, locate, matches,
                                     parse, text)
from market_prices.numbers import NumberFormat, parse_number
from market_prices.schema import ListingFacts, MalformedListing, PriceStatus
from market_prices.text import on_site
from stone_pipeline.core.text import match_key

if TYPE_CHECKING:
    from market_prices.register import Site

_NO_RECORD = "the product page holds no declared record"
_MANY_SYMBOLS = "price text holds more than one declared currency symbol"
_PART_OF_NUMBER = "{field} capture is part of a longer number"
_ISO_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class _Record:
    page_url: str
    element: Element


@dataclass(frozen=True)
class _Skipped:
    """A product page that gives no listing, and why: counted against the site, the other pages still count."""

    reason: str


def _records(page_url: str, page: str, declaration: HtmlDeclaration) -> list[_Record]:
    record = declaration.record
    return [_Record(page_url, element) for element in descendants(parse(page))
            if matches(element, record.tag, record.class_, record.attribute)]


def _no_constant(name: str) -> None:
    """NaN and Infinity are not values: read as null."""
    return None


def _json_value(record: Element, source: HtmlSource, field: str) -> Optional[str]:
    attribute, path = source.json_
    holder = record if attribute in record.attrs else first(record, attribute=attribute)
    if holder is None:
        return None
    try:
        value = json.loads(holder.attrs[attribute], parse_float=str, parse_int=str, parse_constant=_no_constant)
    except (ValueError, RecursionError) as exc:
        raise MalformedListing(f"{field} JSON in {attribute} does not parse") from exc
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return collapse(value) if isinstance(value, str) else None


def _element(record: Element, source: HtmlSource) -> Optional[tuple[Element, bool]]:
    """The element an attr, text or label source reads, and whether it sits inside struck text: the record itself,
    or the first element under it with the class (a current reading skips struck subtrees)."""
    return (record, False) if source.class_ is None else locate(record, source.class_, source.struck)


def _read(record: Element, source: HtmlSource, field: str) -> Optional[str]:
    """The value as written, before any pattern; None when the record does not hold it."""
    if source.json_ is not None:
        return _json_value(record, source, field)
    found = _element(record, source)
    if found is None:
        return None
    element, inside_struck = found
    if source.attr is not None:
        return collapse(element.attrs.get(source.attr, ""))
    if source.label is not None:
        return labelled(element, source.label)
    return text(element, struck=source.struck, inside_struck=inside_struck)


def _narrowed(value: str, source: HtmlSource) -> Optional[str]:
    """The pattern's group, or the whole value when no pattern is declared; None when the pattern does not match."""
    if source.pattern is None:
        return value
    found = re.search(source.pattern, value)
    return collapse(found.group(1) or "") if found else None


def _required(record: Element, source: HtmlSource, field: str) -> str:
    value = _read(record, source, field)
    if value is None:
        raise MalformedListing(f"{field} not found in the record")
    narrowed = _narrowed(value, source)
    if narrowed is None:
        raise MalformedListing(f"{field} pattern does not match '{value}'")
    return narrowed


def _optional(record: Element, source: Optional[HtmlSource], field: str) -> Optional[str]:
    """A value the listing may leave out: not held, or not in its declared form, is not stated, never a refusal."""
    if source is None:
        return None
    value = _read(record, source, field)
    return None if value is None else _narrowed(value, source)


def _url(record: _Record, source: Optional[HtmlSource], base_url: str) -> str:
    if source is None:
        return record.page_url
    absolute = normal(urljoin(record.page_url, _required(record.element, source, "url")))
    if not on_site(absolute, base_url):
        raise MalformedListing("url is not on the site")
    return absolute


def _joins_digits(price_text: str, at: int, step: int, number: NumberFormat) -> bool:
    """Whether the text at `at` continues a number outward (step -1 before a capture, +1 after it): a digit, or a
    declared separator with a digit beyond it."""
    def char(index: int) -> str:
        return price_text[index] if 0 <= index < len(price_text) else ""
    separators = {number.decimal, number.thousands} - {""}
    return char(at).isdigit() or (char(at) in separators and char(at + step).isdigit())


def _amount(price_text: str, source: HtmlSource, field: str,
            declaration: HtmlDeclaration) -> tuple[Optional[str], tuple[str, ...]]:
    """(a price's amount, notes): its source's pattern group read in the site's declared notation, a positive exact
    decimal; any other form is no amount (the price text is kept). A capture must be a whole number: one that
    continues into more digits (directly or across a declared separator) is part of a number, never its amount, so a
    row's pattern can never read "50" out of "120.50"; the note says so and is counted."""
    if source.pattern is not None:
        found = re.search(source.pattern, price_text)
        if found is not None and found.group(1) and (
                _joins_digits(price_text, found.start(1) - 1, -1, declaration.number)
                or _joins_digits(price_text, found.end(1), 1, declaration.number)):
            return None, (_PART_OF_NUMBER.format(field=field),)
    captured = _narrowed(price_text, source)
    number = parse_number(captured, declaration.number) if captured is not None else None
    return (format(number, "f") if number is not None and number > 0 else None), ()


def _whole(symbol: str, text: str) -> bool:
    """Whether the symbol occurs in the text whole: not joined to a letter on either side, so "R$" and "CA$" never
    count as a declared "$"."""
    at = text.find(symbol)
    while at != -1:
        before, after = text[at - 1:at], text[at + len(symbol):at + len(symbol) + 1]
        if not before.isalpha() and not after.isalpha():
            return True
        at = text.find(symbol, at + 1)
    return False


def _currency(record: Element, declaration: HtmlDeclaration, price_text: str) -> tuple[Optional[str], tuple[str, ...]]:
    """(the currency, notes). A currency source must read an ISO 4217 code; a symbol counts only whole, and price text
    naming two declared currencies gives none. Neither is ever guessed: the listing keeps its price, the note says
    why its currency is empty."""
    fields = declaration.fields
    if fields.currency is not None:
        code = _optional(record, fields.currency, "currency")
        if code is not None and not _ISO_CURRENCY.fullmatch(code):
            return None, (f"currency '{code}' is not an ISO 4217 code",)
        return code, ()
    codes = {code for symbol, code in fields.currency_symbols.items() if _whole(symbol, price_text)}
    if len(codes) > 1:
        return None, (_MANY_SYMBOLS,)
    return next(iter(codes), None), ()


def _specs(record: Element, declaration: HtmlDeclaration) -> dict[str, str]:
    return {label: value for label, source in declaration.specs.items()
            if (value := _optional(record, source, label)) is not None}


def _grade(specs: dict[str, str], labels: tuple[str, ...]) -> Optional[str]:
    wanted = {label.lower() for label in labels}
    return next((value for label, value in sorted(specs.items()) if label.lower() in wanted), None)


class DeclaredHtmlCollector:
    platform = "declared_html"
    version = "declared_html-1"
    takes_query = False
    needs_discovery = True
    needs_html = True
    reserved_query: frozenset = frozenset()

    def listings(self, site: "Site", fetcher: SiteFetcher) -> Iterator[object]:
        declaration = site.html
        if declaration.read == "listing_pages":
            for url, page, _links in listing_pages(site, fetcher):
                try:
                    records = _records(url, page, declaration)
                except PageTooLarge as exc:
                    raise UnexpectedResponse(f"{url}: {exc}") from exc
                yield from records
            return
        urls = product_urls(site, fetcher)
        if not urls:
            raise UnexpectedResponse("the entry points list no product page matching product_path")
        cap = config.SCHEMA_ORG_MAX_PRODUCT_PAGES
        for url in urls[:cap]:
            yield from self._product_page(url, fetcher, declaration)
        if len(urls) > cap:
            raise UnexpectedResponse(f"the entry points list {len(urls)} product pages, over the cap of {cap}: "
                                     f"the first {cap} in sorted order were read")

    @staticmethod
    def _product_page(url: str, fetcher: SiteFetcher, declaration: HtmlDeclaration) -> Iterator[object]:
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
            records = _records(url, page.text, declaration)
        except PageTooLarge as exc:
            yield _Skipped(str(exc))
            return
        if not records:
            yield _Skipped(_NO_RECORD)
            return
        yield from records

    def listing_text(self, raw: object, site: "Site") -> str:
        """The record's listing text: the text the run's scope is decided on, before anything else is read."""
        if isinstance(raw, _Skipped):
            raise MalformedListing(raw.reason)
        if not isinstance(raw, _Record):
            raise MalformedListing("not a declared record")
        return _required(raw.element, site.html.fields.listing_text, "listing_text")

    def facts(self, raw: object, site: "Site") -> ListingFacts:
        name = self.listing_text(raw, site)
        declaration = site.html
        fields, record = declaration.fields, raw.element
        price_text = _read(record, fields.price, "price") if fields.price is not None else None
        amount, price_notes = (_amount(price_text, fields.price, "price", declaration) if price_text is not None
                               else (None, ()))
        regular_text = _read(record, fields.regular_price, "regular_price") if fields.regular_price else None
        regular_amount, regular_notes = (_amount(regular_text, fields.regular_price, "regular_price", declaration)
                                         if regular_text is not None else (None, ()))
        on_request = (amount is None and price_text is not None
                      and match_key(price_text) in {match_key(word) for word in fields.on_request})
        specs = _specs(record, declaration)
        currency, currency_notes = _currency(record, declaration, price_text) if amount is not None else (None, ())
        return ListingFacts(
            listing_key=_required(record, fields.listing_key, "listing_key"),
            source_url=_url(raw, fields.url, site.base_url),
            listing_text=name,
            stated_specs=specs,
            grade_text=_grade(specs, site.grade_labels),
            price_text=price_text,
            price_amount=amount,
            regular_price_text=regular_text,
            regular_price_amount=regular_amount,
            currency=currency,
            price_status=(PriceStatus.priced if amount is not None
                          else PriceStatus.on_request if on_request else PriceStatus.none),
            unit_text=_optional(record, fields.unit, "unit"),
            notes=price_notes + regular_notes + currency_notes,
        )
