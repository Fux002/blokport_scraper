"""The per-brand sheet layouts: which competitor price sheets the intake can read, and how each column maps onto the
observation row. A file is read only through a layout whose header row it matches exactly; nothing is guessed."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator

from market_prices import config
from market_prices.register import SLUG, Country, Register, Words, supplier_keys, supplier_problem
from market_prices.schema import DeliveryIncluded, SellerType, VatBasis

Currency = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
# A bare host such as "www.stonecorp.co.uk": lowercase labels, no scheme, path, port or spaces.
Host = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")]
_CELL = re.compile(r"([A-Z]{1,3})([1-9][0-9]*)")
_SPACE = re.compile(r"\s+")


class LayoutError(Exception):
    """The layout file is missing or does not validate: no sheet is read on a half-read file."""


def name_key(text: str) -> str:
    """How a header or a competitor name is compared: case and spacing do not count."""
    return _SPACE.sub(" ", text).strip().casefold()


def cell_position(ref: str) -> tuple[int, int]:
    """An A1 reference as a 0-based (row, column)."""
    match = _CELL.fullmatch(ref)
    if not match:
        raise ValueError(f"cell {ref!r} is not an A1 reference")
    column = 0
    for letter in match.group(1):
        column = column * 26 + ord(letter) - ord("A") + 1
    return int(match.group(2)) - 1, column - 1


class Competitor(BaseModel):
    """A seller a sheet can name, with the seller fields every row needs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    competitor_id: str
    names: tuple[Words, ...] = Field(min_length=1)
    seller_type: SellerType
    seller_country: Country
    served_market: Country
    # The competitor's own site host, and the product scraper's source id when it is also one of the owner's
    # suppliers: checked both ways against the suppliers at load (load_layouts), by the register's rule.
    site_domain: Host
    supplier_key: Optional[Words] = None

    @field_validator("competitor_id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError(f"competitor_id {value!r} must match {SLUG.pattern}")
        return value


class NameSource(BaseModel):
    """Where the competitor's name is: one cell, a column (each row its own), or the file name (group `name`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cell: Optional[str] = None
    column: Optional[Words] = None
    filename: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "NameSource":
        given = [name for name in ("cell", "column", "filename") if getattr(self, name) is not None]
        if len(given) != 1:
            raise ValueError(f"competitor names exactly one of cell, column, filename (got {given or 'none'})")
        if self.cell is not None:
            cell_position(self.cell)
        if self.filename is not None:
            try:
                pattern = re.compile(self.filename)
            except re.error as exc:
                raise ValueError(f"filename pattern {self.filename!r} does not compile: {exc}") from exc
            if "name" not in pattern.groupindex:
                raise ValueError(f"filename pattern {self.filename!r} has no group named 'name'")
        return self


class NumberFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decimal: Literal[".", ","]
    thousands: Literal["", ",", ".", " ", "'"]

    @model_validator(mode="after")
    def _separators_differ(self) -> "NumberFormat":
        if self.decimal == self.thousands:
            raise ValueError(f"the decimal and thousands separators are both {self.decimal!r}")
        return self


class SheetStatements(BaseModel):
    """What a sheet says once for all its rows, such as "all prices in GBP excluding VAT"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: Optional[Currency] = None
    vat_basis: Optional[VatBasis] = None
    unit_text: Optional[Words] = None
    delivery_text: Optional[Words] = None
    delivery_included: Optional[DeliveryIncluded] = None


class Layout(BaseModel):
    """One competitor sheet layout. `headers` is the exact header row, its signature."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layout_id: str
    sheet: Optional[Words] = None                      # an xlsx worksheet's name; a csv layout has none
    header_row: int = Field(ge=1)
    headers: tuple[Words, ...] = Field(min_length=1)
    competitor: NameSource
    listing_key: tuple[Words, ...] = Field(min_length=1)
    listing_text: tuple[Words, ...] = Field(min_length=1)
    # The price column: `price` for the sheet, or `price_by_competitor` when a sheet names several competitors and
    # each states its price in its own column (competitor_id -> column). Exactly one of the two.
    price: Optional[Words] = None
    price_by_competitor: dict[str, Words] = {}
    unit: Optional[Words] = None
    currency: Optional[Words] = None
    min_qty: Optional[Words] = None
    availability: Optional[Words] = None
    grade: Optional[Words] = None
    number: NumberFormat
    statements: SheetStatements = SheetStatements()
    on_request_words: tuple[Words, ...] = ()
    currency_symbols: dict[Words, Currency] = {}

    @field_validator("layout_id")
    @classmethod
    def _id_is_a_slug(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError(f"layout_id {value!r} must match {SLUG.pattern}")
        return value

    @model_validator(mode="after")
    def _columns_are_headers(self) -> "Layout":
        keys = [name_key(header) for header in self.headers]
        if len(set(keys)) != len(keys):
            raise ValueError(f"layout {self.layout_id}: a header appears twice in {list(self.headers)}")
        if self.price is not None and self.price_by_competitor:
            raise ValueError(f"layout {self.layout_id}: price or price_by_competitor, not both")
        if self.price is None and not self.price_by_competitor:
            raise ValueError(f"layout {self.layout_id} names no price column (price or price_by_competitor)")
        named = (self.price, self.unit, self.currency, self.min_qty, self.availability, self.grade,
                 self.competitor.column)
        for column in (*self.listing_key, *self.listing_text, *self.price_by_competitor.values(),
                       *(column for column in named if column is not None)):
            if name_key(column) not in keys:
                raise ValueError(f"layout {self.layout_id}: column {column!r} is not a header")
        return self

    def price_column(self, competitor_id: str) -> Optional[str]:
        """The column a row of this competitor states its price in; None when the layout gives it none."""
        return self.price if self.price is not None else self.price_by_competitor.get(competitor_id)

    def field_columns(self, competitor_id: str) -> tuple[str, ...]:
        """Every header read into an observation field for a row of `competitor_id` (its own price column); the
        others become stated_specs. The identity columns (the competitor's name column, the listing_key parts) are
        not fields: a consumer reads thickness or finish from stated_specs and never parses listing_key."""
        single = (self.price_column(competitor_id), self.unit, self.currency, self.min_qty, self.availability,
                  self.grade)
        return (*self.listing_text, *(column for column in single if column is not None))

    @property
    def signature(self) -> tuple[str, ...]:
        return tuple(name_key(header) for header in self.headers)


class Layouts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand: str
    competitors: tuple[Competitor, ...]
    layouts: tuple[Layout, ...]

    @model_validator(mode="after")
    def _unique(self) -> "Layouts":
        _refuse_repeats("competitor_id", [competitor.competitor_id for competitor in self.competitors])
        _refuse_repeats("competitor name", [name_key(name) for c in self.competitors for name in c.names])
        _refuse_repeats("layout_id", [layout.layout_id for layout in self.layouts])
        _refuse_repeats("header signature", [layout.signature for layout in self.layouts])
        declared = {competitor.competitor_id for competitor in self.competitors}
        for layout in self.layouts:
            if unknown := sorted(set(layout.price_by_competitor) - declared):
                raise ValueError(f"layout {layout.layout_id}: price_by_competitor names {unknown}, "
                                 f"not a declared competitor")
        return self

    def competitor_named(self, name: str) -> Optional[Competitor]:
        wanted = name_key(name)
        return next((c for c in self.competitors if wanted in {name_key(n) for n in c.names}), None)


def _refuse_repeats(what: str, values: list) -> None:
    if repeated := sorted({str(value) for value in values if values.count(value) > 1}):
        raise ValueError(f"duplicate {what}: {repeated}")


def check_ids_apart(layouts: Layouts, register: Register) -> None:
    """A competitor id is a site_id in the delivered rows, so it must not be one of the brand's sites."""
    if shared := sorted({c.competitor_id for c in layouts.competitors} & {site.site_id for site in register.sites}):
        raise LayoutError(f"competitor_id also a site_id in the {register.brand} register: {shared}")


def load_layouts(brand: str, layouts_dir: Path = config.LAYOUTS_DIR) -> Layouts:
    """The validated layouts of one brand. Any problem raises, so no file is read on a half-read layout file."""
    if not SLUG.fullmatch(brand):
        raise LayoutError(f"brand {brand!r} must match {SLUG.pattern}")
    path = layouts_dir / f"{brand}.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LayoutError(f"no sheet layouts for brand {brand!r} at {path}") from exc
    except yaml.YAMLError as exc:
        raise LayoutError(f"{path} is not valid YAML: {exc}") from exc
    try:
        layouts = Layouts.model_validate(document)
    except ValidationError as exc:
        raise LayoutError(f"{path} does not validate: {exc}") from exc
    if layouts.brand != brand:
        raise LayoutError(f"{path} declares brand {layouts.brand!r}")
    suppliers = supplier_keys()
    for competitor in layouts.competitors:
        if problem := supplier_problem(f"{path}: competitor {competitor.competitor_id}", competitor.site_domain,
                                       competitor.supplier_key, suppliers):
            raise LayoutError(problem)
    return layouts
