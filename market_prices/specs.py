"""A listing's thickness, finish, material and origin as normalised columns (contract v3, CONTRACT.md "Specs"). Each
source names the labels that state its specs in its own config (a register row's `spec_labels`, a sheet layout's
`specs`); words map through the brand's vocabulary and countries through one brand-neutral table. A stated value with no
honest mapping refuses its row (MalformedListing, with the reason the site status counts): never a guess."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal, Mapping, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from market_prices import config
from market_prices.schema import SPEC_TEXTS, MalformedListing, Provenance
from market_prices.vocabulary import Vocabulary, Word
from stone_pipeline.core.text import match_key

# One number, a dot or a comma as its decimal mark, then mm, cm or nothing: anything else is not one thickness.
_THICKNESS = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(mm|cm)?$", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_MM_PER_UNIT = {"mm": Decimal(1), "cm": Decimal(10)}
# How a site status counts the values that were one of the brand's unstated words (CONTRACT.md "Specs").
UNSTATED_NOTE = "stated as unstated"


class SpecError(Exception):
    """The country table is missing or does not validate: no spec is read on a half-read table."""


def label_key(label: str) -> str:
    """How a seller's label is compared: case and spacing do not count (as a sheet header is, layouts.name_key)."""
    return _SPACE.sub(" ", label).strip().casefold()


# --- where a source states its specs ----------------------------------------------------------------------------------
class SpecSource(BaseModel):
    """The labels (or sheet headers) that state one spec, in the order they are read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    labels: tuple[Word, ...] = Field(min_length=1)


class ThicknessSource(SpecSource):
    unit: Optional[Literal["mm", "cm"]] = None      # the unit of a bare number; a unit stated in the value wins


class SpecLabels(BaseModel):
    """One source's spec map: a register row's `spec_labels` or a sheet layout's `specs`. A spec it leaves out is
    delivered empty."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thickness: Optional[ThicknessSource] = None
    finish: Optional[SpecSource] = None
    material_type: Optional[SpecSource] = None
    origin: Optional[SpecSource] = None

    def all_labels(self) -> tuple[str, ...]:
        return tuple(label for source in (self.thickness, self.finish, self.material_type, self.origin)
                     if source is not None for label in source.labels)


# --- the country table ------------------------------------------------------------------------------------------------
class Country(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    iso2: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
    iso3: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
    names: tuple[Word, ...] = Field(min_length=1)


class Countries(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    countries: tuple[Country, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _each_code_and_name_once(self) -> "Countries":
        codes = [code for country in self.countries for code in (country.iso2, country.iso3)]
        if repeated := sorted({code for code in codes if codes.count(code) > 1}):
            raise ValueError(f"country codes {repeated} are declared twice")
        names = [(match_key(name), name) for country in self.countries for name in country.names]
        keys = [key for key, _ in names]
        if repeated := sorted({name for key, name in names if keys.count(key) > 1}):
            raise ValueError(f"country names {repeated} are declared twice")
        return self

    def code_for(self, stated: str) -> Optional[str]:
        """The lower-case ISO alpha-2 code a stated origin names: a code as ISO writes it, or a name; else None."""
        text = stated.strip()
        key = match_key(text)
        for country in self.countries:
            if text in (country.iso2, country.iso3) or key in {match_key(name) for name in country.names}:
                return country.iso2.lower()
        return None


def load_countries(path: Path = config.COUNTRIES_FILE) -> Countries:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpecError(f"no country table at {path}") from exc
    except yaml.YAMLError as exc:
        raise SpecError(f"{path} is not valid YAML: {exc}") from exc
    try:
        return Countries.model_validate(document)
    except ValidationError as exc:
        raise SpecError(f"{path} does not validate: {exc}") from exc


# --- one listing's specs ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Specs:
    """One listing's spec columns. `texts` and `labels` are keyed by column; `used` holds the stated labels read into a
    column (they leave stated_specs); `unstated` counts the values that were one of the brand's unstated words."""

    thickness_mm: Optional[str] = None
    finish: Optional[str] = None
    material_type: Optional[str] = None
    origin_iso_2: Optional[str] = None
    texts: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    used: frozenset[str] = frozenset()
    unstated: int = 0

    def columns(self) -> dict[str, object]:
        """The row's nine v3 columns: each spec, each as written, and the labels they were read from."""
        values: dict[str, object] = {column: getattr(self, column) for column in SPEC_TEXTS}
        values.update({text: self.texts.get(column) for column, text in SPEC_TEXTS.items()})
        values["spec_labels"] = dict(self.labels)
        return values

    def provenance(self) -> dict[str, Provenance]:
        """`listing` for each stated spec, `unknown` for each empty one."""
        return {column: Provenance.listing if column in self.texts else Provenance.unknown for column in SPEC_TEXTS}


NO_SPECS = Specs()


def read_specs(stated: Mapping[str, str], labels: Optional[SpecLabels], vocabulary: Vocabulary,
               countries: Countries) -> Specs:
    """The listing's spec columns from its stated label -> value pairs, through its source's labels."""
    if labels is None:
        return NO_SPECS
    by_key: dict[str, list[tuple[str, str]]] = {}
    for label, value in stated.items():
        if value and value.strip():
            by_key.setdefault(label_key(label), []).append((label, value.strip()))
    values: dict[str, str] = {}
    texts: dict[str, str] = {}
    read_from: dict[str, str] = {}
    unstated = 0
    plan = (("thickness_mm", labels.thickness), ("finish", labels.finish),
            ("material_type", labels.material_type), ("origin_iso_2", labels.origin))
    for column, source in plan:
        if source is None:
            continue
        found = _stated(column, source, by_key)
        if found is None:
            continue
        label, text = found
        if vocabulary.is_unstated(text):
            unstated += 1
            continue
        values[column] = _normalised(column, text, source, vocabulary, countries)
        texts[column], read_from[column] = text, label
    return Specs(thickness_mm=values.get("thickness_mm"), finish=values.get("finish"),
                 material_type=values.get("material_type"), origin_iso_2=values.get("origin_iso_2"),
                 texts=texts, labels=read_from, used=frozenset(read_from.values()), unstated=unstated)


def _stated(column: str, source: SpecSource, by_key: dict[str, list[tuple[str, str]]]) -> Optional[tuple[str, str]]:
    """The one (label, value) the listing states for this spec; two labels stating different values refuse it."""
    found = [pair for label in source.labels for pair in by_key.get(label_key(label), [])]
    if not found:
        return None
    if differing := sorted({value for _, value in found} - {found[0][1]}):
        raise MalformedListing(f"{column}: two labels state different values '{found[0][1]}' / '{differing[0]}'")
    return found[0]


def _normalised(column: str, text: str, source: SpecSource, vocabulary: Vocabulary, countries: Countries) -> str:
    if column == "thickness_mm":
        return _millimetres(text, source.unit if isinstance(source, ThicknessSource) else None)
    if column == "finish":
        if (finish := vocabulary.finish_for(text)) is None:
            raise MalformedListing(f"finish '{text}' is not in {vocabulary.brand}'s finishes")
        return finish
    if column == "material_type":
        if (material := vocabulary.material_for(text)) is None:
            raise MalformedListing(f"material '{text}' is not in {vocabulary.brand}'s materials")
        return material
    if (code := countries.code_for(text)) is None:
        raise MalformedListing(f"origin '{text}' is not in the country table")
    return code


def _millimetres(text: str, declared: Optional[str]) -> str:
    """One number in mm or cm as an exact mm decimal with no trailing zeros ("20", "12.5")."""
    match = _THICKNESS.fullmatch(text)
    unit = (match.group(2) or declared or "").lower() if match else ""
    try:
        number = Decimal(match.group(1).replace(",", ".")) if match else None
    except InvalidOperation:
        number = None
    if number is None or not unit or number <= 0:
        raise MalformedListing(f"thickness '{text}' is not one number in mm or cm")
    millimetres = (number * _MM_PER_UNIT[unit]).normalize()
    return format(millimetres, "f")
