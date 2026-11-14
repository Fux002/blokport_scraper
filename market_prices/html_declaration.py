"""A register row's `html` declaration for the declared-HTML collector (platform declared_html, CONTRACT.md "Adding a
site"): where its pages hold each listing's facts, as `discovery` says where the pages are. One logic for every such
site: the row declares, the collector reads; nothing is per-site code. Each value is a SOURCE inside one record:
exactly one of a JSON attribute path, an attribute, or a text, optionally narrowed by a one-group pattern."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from market_prices.numbers import NumberFormat
from market_prices.vocabulary import Word

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]   # ISO 4217, as a sheet layout declares it


class HtmlSource(BaseModel):
    """Where one value sits in a record. `json`: [attribute, dotted.path] into a JSON-valued attribute of the record or
    of the first element under it that carries the attribute. `attr`: an attribute of the record, or of the first
    element under it with `class`. `text`: the text of the record, or of the first element under it with `class`;
    with `struck`, only its struck text (a former price). `label`: the value the record, or the first element under it
    with `class`, pairs with that label (a th/td row, a dt/dd pair, a label element followed by its text), the label
    matched whole, case, colon and whitespace aside. `pattern`'s one group narrows the value as written."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    json_: Optional[tuple[Word, Word]] = Field(default=None, alias="json")
    attr: Optional[Word] = None
    text: bool = False
    label: Optional[Word] = None
    class_: Optional[Word] = Field(default=None, alias="class")
    pattern: Optional[str] = None
    struck: bool = False

    @model_validator(mode="after")
    def _one_kind(self) -> "HtmlSource":
        kinds = [name for name, given in (("json", self.json_ is not None), ("attr", self.attr is not None),
                                          ("text", self.text), ("label", self.label is not None)) if given]
        if len(kinds) != 1:
            raise ValueError(f"a source names exactly one of json, attr, text, label (got {kinds or 'none'})")
        if self.json_ is not None and self.class_ is not None:
            raise ValueError("a json source finds its attribute itself: it takes no class")
        if self.struck and not self.text:
            raise ValueError("struck reads an element's struck text: it needs text")
        if self.pattern is not None:
            try:
                groups = re.compile(self.pattern).groups
            except re.error as exc:
                raise ValueError(f"pattern {self.pattern!r} does not compile: {exc}") from exc
            if groups != 1:
                raise ValueError(f"pattern {self.pattern!r} needs exactly one capture group (has {groups})")
        return self


class HtmlRecord(BaseModel):
    """What one listing is on a page: every element with the class token (and the tag, when given), or every element
    carrying the attribute."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    tag: Optional[Word] = None
    class_: Optional[Word] = Field(default=None, alias="class")
    attribute: Optional[Word] = None

    @model_validator(mode="after")
    def _one_selector(self) -> "HtmlRecord":
        if (self.class_ is None) == (self.attribute is None):
            raise ValueError("a record names exactly one of class, attribute")
        return self


class HtmlFields(BaseModel):
    """The listing's facts. The price's element text is price_text as shown; its amount is the pattern's group read in
    the site's declared number notation. `currency_symbols` names the code a symbol in the price text stands for (a
    symbol counts only whole, not joined to a letter, so "R$" is never "$"; two currencies in one text give none);
    `on_request` is the price texts that mean the price is on request, as the page states them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    listing_key: HtmlSource
    listing_text: HtmlSource
    url: Optional[HtmlSource] = None
    price: Optional[HtmlSource] = None
    # contract v4 (Mas's sale rule): the regular price shown beside the current one, typically its struck "was" price
    regular_price: Optional[HtmlSource] = None
    unit: Optional[HtmlSource] = None
    currency: Optional[HtmlSource] = None
    currency_symbols: dict[Word, Currency] = {}
    on_request: tuple[Word, ...] = ()

    @model_validator(mode="after")
    def _price_is_visible_text(self) -> "HtmlFields":
        """F1: what a page shows is what is read. A JSON or attribute value (an analytics or data attribute) is never
        the price; they stay for identity and specs."""
        if self.price is not None and not self.price.text:
            raise ValueError("the price is the page's visible text: a price source takes text, never json, attr or "
                             "label")
        if self.regular_price is not None and not self.regular_price.text:
            raise ValueError("the regular price is the page's visible text: it takes text, never json, attr or label")
        return self

    @model_validator(mode="after")
    def _struck_is_never_a_fact(self) -> "HtmlFields":
        struck = [name for name in ("listing_key", "listing_text", "url", "price", "unit", "currency")
                  if (source := getattr(self, name)) is not None and source.struck]
        if struck:
            raise ValueError(f"struck text is never the price or any listing fact {struck}: it is read only as the "
                             f"regular_price")
        return self


class HtmlDeclaration(BaseModel):
    """Where a declared_html site's listings are (`read`: on its listing pages, or on each product page its discovery
    reaches), what one listing is (`record`), where each fact and stated spec sits in it, and the site's number
    notation (`number`, the sheet layouts' model: numbers.NumberFormat)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read: Literal["listing_pages", "product_pages"]
    record: HtmlRecord
    fields: HtmlFields
    number: Optional[NumberFormat] = None
    specs: dict[Word, HtmlSource] = {}

    @model_validator(mode="after")
    def _whole(self) -> "HtmlDeclaration":
        if (self.fields.price is not None or self.fields.regular_price is not None) and self.number is None:
            raise ValueError("a declared price needs the site's number notation (number: {decimal, thousands})")
        if struck := sorted(label for label, source in self.specs.items() if source.struck):
            raise ValueError(f"specs {struck} read struck text: it is read only as the regular_price")
        return self
