"""The per-brand site register, and the gate: a site is collected only on a recorded, complete decision."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, StringConstraints, ValidationError, field_validator, model_validator,
)

from market_prices import config
from market_prices.collectors import COLLECTORS
from market_prices.schema import DeliveryIncluded, SellerType, VatBasis
from market_prices.text import same_host, site_host

SLUG = re.compile(r"[a-z0-9_]+")


def _refuse_number(value: object) -> object:
    if isinstance(value, (int, float)):
        raise ValueError("a date must be written as a date, not a number")
    return value


Day = Annotated[date, BeforeValidator(_refuse_number)]
Words = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Country = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]


class RegisterError(Exception):
    """The register cannot be used. Nothing is collected from a register that does not validate."""


class Decision(str, Enum):
    approved = "approved"
    pending = "pending"
    refused = "refused"


class Terms(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    terms_url: Optional[str] = None
    checked_on: Optional[Day] = None
    summary: Optional[Words] = None

    @field_validator("terms_url")
    @classmethod
    def _terms_url_is_https(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not (urlparse(value).scheme == "https" and urlparse(value).hostname):
            raise ValueError(f"terms_url {value!r} must be an https URL")
        return value

    @property
    def complete(self) -> bool:
        return all((self.terms_url, self.checked_on, self.summary))


class SiteStatements(BaseModel):
    """What a site says once for all its listings, such as "all prices include VAT"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vat_basis: Optional[VatBasis] = None
    unit_text: Optional[Words] = None
    delivery_text: Optional[Words] = None
    delivery_included: Optional[DeliveryIncluded] = None


class Site(BaseModel):
    """One storefront. A site selling into several markets is one entry per market."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: str
    seller_name: Words
    seller_type: SellerType
    seller_country: Optional[Country] = None
    served_market: Optional[Country] = None
    platform: str
    base_url: str
    query: dict[str, str] = {}
    terms: Terms
    decision: Decision
    decided_by: Optional[Words] = None
    decided_on: Optional[Day] = None
    statements: SiteStatements = SiteStatements()
    grade_labels: tuple[Words, ...] = ()
    # The product scraper's source id when this seller is also one of the owner's suppliers (check_suppliers).
    supplier_key: Optional[Words] = None

    @field_validator("site_id")
    @classmethod
    def _site_id_is_a_slug(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError(f"site_id {value!r} must match {SLUG.pattern}")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_is_a_bare_https_origin(cls, value: str) -> str:
        parsed = urlparse(value)
        if not parsed.hostname or "@" in parsed.netloc or value != f"https://{parsed.netloc}":
            raise ValueError(f"base_url {value!r} must be a bare https origin")
        return value

    @model_validator(mode="after")
    def _platform_and_gate(self) -> "Site":
        collector = COLLECTORS.get(self.platform)
        if collector is None:
            raise ValueError(f"unknown platform {self.platform!r}; known: {sorted(COLLECTORS)}")
        if reserved := sorted(collector.reserved_query & set(self.query)):
            raise ValueError(f"query may not set {reserved}: the collector owns them")
        if self.decision is Decision.approved:
            self._require_full_record()
        return self

    def _require_full_record(self) -> None:
        if not self.terms.complete:
            raise ValueError("approved needs terms_url, checked_on and summary")
        if not self.decided_by or not self.decided_on:
            raise ValueError("approved needs decided_by and decided_on")
        if self.decided_on < self.terms.checked_on:
            raise ValueError("approved needs decided_on on or after checked_on")
        if self.decided_on > datetime.now(timezone.utc).date():
            raise ValueError("approved needs dates that are not in the future")
        if not self.seller_country or not self.served_market:
            raise ValueError("approved needs seller_country and served_market")

    @property
    def collectable(self) -> bool:
        return self.decision is Decision.approved

    @property
    def robots_url(self) -> str:
        return f"{self.base_url}{config.ROBOTS_PATH}"


class Register(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand: str
    sites: tuple[Site, ...]

    @model_validator(mode="after")
    def _site_ids_unique(self) -> "Register":
        ids = [site.site_id for site in self.sites]
        if duplicates := sorted({site_id for site_id in ids if ids.count(site_id) > 1}):
            raise ValueError(f"duplicate site_id: {duplicates}")
        return self


def supplier_problem(label: str, host: str, supplier_key: Optional[str], domains: dict[str, str]) -> Optional[str]:
    """Why a seller's supplier flag is wrong, or None. The flag is written in the register (or the layout file) and
    checked both ways against the product scraper's suppliers: the key is a supplier's source id, its domain is the
    seller's host, and a supplier's domain never goes without the key. `domains` is scrapers.run.supplier_domains()
    (source id -> site_domain). The one rule for a register site and a price sheet's competitor."""
    if supplier_key is not None:
        if supplier_key not in domains:
            return (f"{label}: supplier_key {supplier_key!r} is not one of the product scraper's suppliers "
                    f"{sorted(domains)}")
        if not same_host(host, domains[supplier_key]):
            return f"{label}: {host} is not supplier {supplier_key}'s domain {domains[supplier_key]}"
        return None
    if matched := sorted(key for key, domain in domains.items() if same_host(host, domain)):
        return f"{label}: {host} is the domain of supplier {matched[0]}; set supplier_key: {matched[0]}"
    return None


def check_suppliers(register: Register, domains: dict[str, str]) -> None:
    """Every site's supplier flag, by supplier_problem."""
    for site in register.sites:
        if problem := supplier_problem(f"site {site.site_id}", site_host(site.base_url), site.supplier_key, domains):
            raise RegisterError(problem)


def supplier_keys() -> dict[str, str]:
    """The product scraper's suppliers and their domains. Imported here, not at the top: only a register or layout
    load needs the fetchers, and every fetcher module is imported to read them."""
    from scrapers.run import supplier_domains
    return supplier_domains()


def load_register(brand: str, sites_dir: Path = config.SITES_DIR) -> Register:
    """The validated register of one brand. Any problem raises, so no site runs on a half-read register."""
    if not SLUG.fullmatch(brand):
        raise RegisterError(f"brand {brand!r} must match {SLUG.pattern}")
    path = sites_dir / f"{brand}.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegisterError(f"no site register for brand {brand!r} at {path}") from exc
    except yaml.YAMLError as exc:
        raise RegisterError(f"{path} is not valid YAML: {exc}") from exc
    try:
        register = Register.model_validate(document)
    except ValidationError as exc:
        raise RegisterError(f"{path} does not validate: {exc}") from exc
    if register.brand != brand:
        raise RegisterError(f"{path} declares brand {register.brand!r}")
    check_suppliers(register, supplier_keys())
    return register
