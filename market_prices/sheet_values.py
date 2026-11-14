"""A sheet cell read as the layout declares it: a price only through the declared separators and currency symbols,
a currency only as an ISO code or a declared symbol. The cell's own text is always kept; nothing is inferred."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from market_prices import config
from market_prices.layouts import Layout, name_key
from market_prices.numbers import parse_number
from market_prices.schema import MalformedListing, PriceStatus
from market_prices.text import clean_text

_ISO_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True)
class Price:
    text: Optional[str]
    amount: Optional[str]
    status: PriceStatus
    currency: Optional[str]          # from a declared symbol in the price cell itself


def cell_text(value: object) -> Optional[str]:
    """A cell as one clean line of text; None for an empty cell. A spreadsheet's own types are written plainly."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return clean_text(value)


def read_price(value: object, layout: Layout) -> Price:
    text = cell_text(value)
    if text is None:
        return Price(None, None, PriceStatus.none, None)
    if isinstance(value, bool):
        return Price(text, None, PriceStatus.none, None)
    if isinstance(value, (int, float)):
        amount = _amount(Decimal(str(value))) if math.isfinite(value) else None
        return Price(text, amount, PriceStatus.priced if amount else PriceStatus.none, None)
    if name_key(text) in {name_key(word) for word in layout.on_request_words}:
        return Price(text, None, PriceStatus.on_request, None)
    number, currency = _without_symbol(text, layout)
    amount = _amount(_parse(number, layout))
    return Price(text, amount, PriceStatus.priced if amount else PriceStatus.none, currency if amount else None)


def read_currency(value: object, layout: Layout) -> Optional[str]:
    """A currency cell: an ISO 4217 code as written, or a symbol the layout declares. Anything else is this row's
    problem, never a guess."""
    text = cell_text(value)
    if text is None:
        return None
    if _ISO_CURRENCY.fullmatch(text):
        return text
    if text in layout.currency_symbols:
        return layout.currency_symbols[text]
    raise MalformedListing(f"currency {text!r} is neither an ISO code nor a symbol the layout declares")


def _without_symbol(text: str, layout: Layout) -> tuple[str, Optional[str]]:
    """The price text with one declared currency symbol taken off its start or end, and that symbol's currency."""
    for symbol, currency in sorted(layout.currency_symbols.items(), key=lambda item: -len(item[0])):
        if text.startswith(symbol):
            return text[len(symbol):].strip(), currency
        if text.endswith(symbol):
            return text[:-len(symbol)].strip(), currency
    return text, None


def _parse(text: str, layout: Layout) -> Optional[Decimal]:
    """The text as a number in the layout's own notation (numbers.parse_number, the one reading)."""
    return parse_number(text, layout.number)


def _amount(number: Optional[Decimal]) -> Optional[str]:
    """A positive number as a plain decimal string; zero (made to measure) or a negative is no price."""
    if number is None or number <= 0:
        return None
    text = format(number, "f")
    if sum(ch.isdigit() for ch in text) > config.SHEET_MAX_DIGITS:
        return None
    return text
