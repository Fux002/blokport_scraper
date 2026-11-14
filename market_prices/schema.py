"""The rows this path delivers. Field order here is the CSV column order, so the shape is declared once."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from market_prices import config
from market_prices.brand import SLUG

# A row's brand is the run's (brand_required): a slug, never empty; write_run refuses any but the run's own.
BRAND_PATTERN = f"^{SLUG.pattern}$"


class MalformedListing(ValueError):
    """One listing cannot be read. It is counted against the site; the other listings still count."""


class Source(str, Enum):
    """Where a row came from: a site this path read, or a price sheet the owner supplied (contract v2)."""

    site = "site"
    file = "file"


class SellerType(str, Enum):
    merchant = "merchant"
    wholesaler = "wholesaler"
    marketplace = "marketplace"
    unknown = "unknown"


class VatBasis(str, Enum):
    incl = "incl"
    excl = "excl"
    unknown = "unknown"


class DeliveryIncluded(str, Enum):
    yes = "yes"
    no = "no"
    unknown = "unknown"


class PriceStatus(str, Enum):
    priced = "priced"
    on_request = "on_request"
    none = "none"


class Provenance(str, Enum):
    listing = "listing"
    site_statement = "site_statement"
    unknown = "unknown"


class SiteRunStatus(str, Enum):
    ok = "ok"
    partial = "partial"
    unavailable_blocked = "unavailable_blocked"
    unavailable_error = "unavailable_error"
    disabled_terms = "disabled_terms"
    disallowed_robots = "disallowed_robots"


class Observation(BaseModel):
    """One listing seen at one site in one run. Null means the site did not say."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str
    source: Source
    brand: str = Field(pattern=BRAND_PATTERN)
    site_id: str
    seller_name: str
    seller_type: SellerType
    seller_country: str = Field(pattern=r"^[A-Z]{2}$")
    served_market: str = Field(pattern=r"^[A-Z]{2}$")
    seller_is_our_supplier: bool
    supplier_key: Optional[str]
    listing_key: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    listing_text: str = Field(min_length=1)
    stated_specs: dict[str, str]
    grade_text: Optional[str]
    price_text: Optional[str]
    price_amount: Optional[str] = Field(pattern=r"^\d+(\.\d+)?$")
    currency: Optional[str] = Field(pattern=r"^[A-Z]{3}$")
    price_status: PriceStatus
    vat_basis: VatBasis
    unit_text: Optional[str]
    delivery_text: Optional[str]
    delivery_included: DeliveryIncluded
    min_qty_text: Optional[str]
    availability_text: Optional[str]
    provenance: dict[str, Provenance]
    observed_at: str
    run_id: str
    collector_version: str

    @model_validator(mode="after")
    def _amount_matches_status(self) -> "Observation":
        if (self.price_status is PriceStatus.priced) != (self.price_amount is not None):
            raise ValueError("price_amount is set exactly when price_status is priced")
        return self

    @model_validator(mode="after")
    def _supplier_flag_has_its_key(self) -> "Observation":
        if self.seller_is_our_supplier != (self.supplier_key is not None):
            raise ValueError("supplier_key is set exactly when seller_is_our_supplier is true")
        return self

    @model_validator(mode="after")
    def _cells_are_deliverable(self) -> "Observation":
        """A row that could not be written or read back is this listing's problem, never the whole run's."""
        for cell in csv_values(self):
            if len(cell) > config.MAX_CELL_CHARS:
                raise ValueError(f"a cell of {len(cell)} characters is over the cap of {config.MAX_CELL_CHARS}")
            cell.encode("utf-8")
        return self


class SiteStatus(BaseModel):
    """What happened at one site in one run: the consumer's evidence for a site with few or no rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: str
    source: Source
    brand: str = Field(pattern=BRAND_PATTERN)
    site_id: str
    status: SiteRunStatus
    rows: int
    malformed_listings: int
    duplicate_listings: int
    detail: str
    terms_checked_on: Optional[str]
    decision: str
    observed_at: str
    run_id: str


@dataclass(frozen=True)
class ListingFacts:
    """What a collector read from one listing. Site identity, statements and provenance are added in one place."""

    listing_key: str
    source_url: str
    listing_text: str
    stated_specs: dict[str, str] = field(default_factory=dict)
    grade_text: Optional[str] = None
    price_text: Optional[str] = None
    price_amount: Optional[str] = None
    currency: Optional[str] = None
    price_status: PriceStatus = PriceStatus.none
    min_qty_text: Optional[str] = None
    availability_text: Optional[str] = None


def columns(model: type[BaseModel]) -> tuple[str, ...]:
    return tuple(model.model_fields)


def csv_values(row: BaseModel) -> list[str]:
    """One row as CSV cells: null is empty, and JSON cells have sorted keys so equal input gives equal bytes."""
    return [_cell(getattr(row, name)) for name in columns(type(row))]


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dict):
        return json.dumps({k: _cell(v) for k, v in value.items()}, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))
    return str(value)
