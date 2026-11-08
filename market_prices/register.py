"""The per-brand site register, and the gate: a site is collected only on a recorded, complete decision."""

from __future__ import annotations

import re
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from market_prices import config
from market_prices.collectors import COLLECTORS
from market_prices.schema import DeliveryIncluded, SellerType, VatBasis
from market_prices.text import on_site

_SLUG = re.compile(r"^[a-z0-9_]+$")


class RegisterError(Exception):
    """The register cannot be used. Nothing is collected from a register that does not validate."""


class Decision(str, Enum):
    approved = "approved"
    pending = "pending"
    refused = "refused"


class Terms(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    terms_url: Optional[str] = None
    robots_url: Optional[str] = None
    checked_on: Optional[date] = None
    summary: Optional[str] = None

    @property
    def complete(self) -> bool:
        return all((self.terms_url, self.robots_url, self.checked_on, self.summary))


class SiteStatements(BaseModel):
    """What a site says once for all its listings, such as "all prices include VAT"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    vat_basis: Optional[VatBasis] = None
    unit_text: Optional[str] = None
    delivery_text: Optional[str] = None
    delivery_included: Optional[DeliveryIncluded] = None


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    site_id: str
    seller_name: str
    seller_type: SellerType
    platform: str
    base_url: str
    query: dict[str, str] = {}
    terms: Terms
    decision: Decision
    decided_by: Optional[str] = None
    decided_on: Optional[date] = None
    statements: SiteStatements = SiteStatements()
    grade_labels: tuple[str, ...] = ()

    @field_validator("site_id")
    @classmethod
    def _site_id_is_a_slug(cls, value: str) -> str:
        if not _SLUG.match(value):
            raise ValueError(f"site_id {value!r} must match {_SLUG.pattern}")
        return value

    @field_validator("base_url")
    @classmethod
    def _base_url_is_a_bare_https_origin(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
            raise ValueError(f"base_url {value!r} must be a bare https origin")
        return value

    @model_validator(mode="after")
    def _platform_and_gate(self) -> "Site":
        collector = COLLECTORS.get(self.platform)
        if collector is None:
            raise ValueError(f"unknown platform {self.platform!r}; known: {sorted(COLLECTORS)}")
        if reserved := sorted(collector.reserved_query & set(self.query)):
            raise ValueError(f"query may not set {reserved}: the collector owns them")
        if self.terms.robots_url and not on_site(self.terms.robots_url, self.base_url):
            raise ValueError(f"robots_url {self.terms.robots_url!r} is not on {self.base_url}")
        if self.decision is Decision.approved:
            if not self.terms.complete:
                raise ValueError("approved needs terms_url, robots_url, checked_on and summary")
            if not self.decided_by or not self.decided_on:
                raise ValueError("approved needs decided_by and decided_on")
        return self

    @property
    def collectable(self) -> bool:
        return self.decision is Decision.approved


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


def load_register(brand: str, sites_dir: Path = config.SITES_DIR) -> Register:
    """The validated register of one brand. Any problem raises, so no site runs on a half-read register."""
    if not _SLUG.match(brand):
        raise RegisterError(f"brand {brand!r} must match {_SLUG.pattern}")
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
    return register
