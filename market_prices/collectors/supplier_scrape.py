"""A supplier's prices read from the product scraper's own fetch of that supplier: one fetch, one transport. This
collector requests nothing. Its listings are the rows of the scraper's separate price pass for the register row's
supplier_key (prices.csv, never products.csv: the product scrape never depends on the prices), and the pass's marker
states whether it read prices (a supplier whose prices need its customer login says why not). Prices the pass did not
read are reported, never delivered as unpriced listings. A price is read as stated:
its currency only from a declared symbol, its amount exact, its unit as written; nothing is converted."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Mapping, Optional

from market_prices import config
from market_prices.fetching import UnexpectedResponse
from market_prices.schema import ListingFacts, MalformedListing, PriceStatus
from market_prices.text import clean_text, on_site

if TYPE_CHECKING:
    from market_prices.register import Site

# A symbol, then one amount (plain, or grouped by commas in threes, a dot as its decimal mark), then "/" and the unit.
_PRICE = re.compile(r"^(?P<symbol>[^\d\s]+)\s*(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                    r"(?:\s*/\s*(?P<unit>\S.*))?$")
_UNDECLARED_SYMBOL = "price in a currency symbol not declared"


@dataclass(frozen=True)
class ScrapeFolder:
    """One run of a supplier scraper's price pass: its price rows, its run folder's UTC stamp, and what its marker
    states about prices ("ok", why they were not read, or None when it states nothing)."""

    source: str
    timestamp: str
    rows: tuple[Mapping[str, str], ...]
    prices: Optional[str]


def read_scrape(folder: Path) -> ScrapeFolder:
    """The price pass in the scraper's run folder <data>/<source>/<stamp>: its rows (none when it read no prices, so
    wrote no prices.csv) and its marker's statement about prices."""
    marker = json.loads((folder / config.SCRAPE_MARKER_FILE).read_text(encoding="utf-8"))
    rows_file = folder / config.SCRAPE_ROWS_FILE
    rows: tuple = ()
    if rows_file.is_file():
        with rows_file.open(encoding="utf-8", newline="") as handle:
            rows = tuple(csv.DictReader(handle))
    prices = marker.get(config.SCRAPE_PRICES_KEY)
    return ScrapeFolder(source=folder.parent.name, timestamp=folder.name, rows=rows,
                        prices=prices if isinstance(prices, str) else None)


@dataclass(frozen=True)
class _Price:
    text: Optional[str] = None
    amount: Optional[str] = None
    currency: Optional[str] = None
    unit: Optional[str] = None
    status: PriceStatus = PriceStatus.none
    notes: tuple[str, ...] = ()


def _price(stated: object) -> _Price:
    """The price as stated: on request, one exact positive amount in a declared currency with its unit, or none with
    its words kept."""
    text = clean_text(stated)
    if text is None:
        return _Price()
    if text.casefold() in config.ON_REQUEST_TEXTS:
        return _Price(text=text, status=PriceStatus.on_request)
    found = _PRICE.match(text)
    if found is None:
        return _Price(text=text)
    amount = found.group("amount").replace(",", "")
    if Decimal(amount) <= 0:
        return _Price(text=text)
    currency = config.CURRENCY_SYMBOLS.get(found.group("symbol"))
    if currency is None:
        return _Price(text=text, notes=(_UNDECLARED_SYMBOL,))
    return _Price(text=text, amount=amount, currency=currency, unit=found.group("unit"), status=PriceStatus.priced)


class SupplierScrapeCollector:
    platform = "supplier_scrape"
    version = "supplier_scrape-1"
    takes_query = False
    needs_discovery = False
    needs_html = False
    needs_scrape = True
    fetches = False
    reserved_query: frozenset = frozenset()

    def listings(self, site: "Site", scrape: ScrapeFolder) -> Iterator[object]:
        """The scrape's rows, when it read prices; else the site reports why not."""
        if scrape.prices != "ok":
            raise UnexpectedResponse(f"{scrape.source}'s scrape of {scrape.timestamp} read no prices: "
                                     f"{scrape.prices or 'its completion marker states no prices'}")
        yield from scrape.rows

    def listing_text(self, raw: object, site: "Site") -> str:
        """The row's text columns, joined as stated: what the run's scope is decided on."""
        row = self._row(raw)
        parts = [clean_text(row.get(column)) for column in site.scrape.text]
        text = " | ".join(part for part in parts if part)
        if not text:
            raise MalformedListing("listing has no text")
        return text

    def facts(self, raw: object, site: "Site") -> ListingFacts:
        text = self.listing_text(raw, site)
        row = self._row(raw)
        key = clean_text(row.get(site.scrape.key))
        if not key:
            raise MalformedListing("listing has no key")
        url = row.get(site.scrape.url)
        if not isinstance(url, str) or not on_site(url, site.base_url):
            raise MalformedListing("listing url is not on the site")
        price = _price(row.get(site.scrape.price))
        specs = {column: value for column in site.scrape.specs if (value := clean_text(row.get(column)))}
        return ListingFacts(listing_key=key, source_url=url, listing_text=text, stated_specs=specs,
                            price_text=price.text, price_amount=price.amount, currency=price.currency,
                            price_status=price.status, unit_text=price.unit, notes=price.notes)

    @staticmethod
    def _row(raw: object) -> Mapping:
        if not isinstance(raw, Mapping):
            raise MalformedListing("scrape row is not a record")
        return raw
