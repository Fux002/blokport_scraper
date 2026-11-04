"""The kind of every name field the scraper writes, read from the backend's field-kinds.json (PHASE-0 N16).

The backend publishes the ONE map; this module is its one reader, so no second list of kinds exists in code. A field
is a NAME (`clean_name`: ASCII letters, digits, spaces, hyphens and apostrophes; displayed title-cased) or an
IDENTIFIER (`clean_identifier`: printable ASCII, punctuation and case kept). There is no default map and no default
kind: an absent, malformed or foreign file, or one that does not list a field the scraper writes, is a load error.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from stone_pipeline.config.settings import BRAND, SETTINGS
from stone_pipeline.core.text import clean_identifier, clean_name, title_case

COLOR_NAME = "color.name"
FINISH_NAME = "finish.name"
PORT_NAME = "port.name"
PORT_REGION_NAME = "port.region_name"
PRODUCT_COLLECTION_CITY = "product.collection_city"
PRODUCT_LOT_CODE = "product.lot_code"
PRODUCT_ORIGIN_CITY = "product.origin_city"
PRODUCT_ORIGIN_COUNTY = "product.origin_county"
PRODUCT_TITLE = "product.title"
PRODUCT_CATEGORY_NAME = "product_category.name"
QUALITY_NAME = "quality.name"
TYPE_NAME = "type.name"
VARIATION_NAME = "variation.name"
VARIATION_ALIAS_VALUE = "variation_alias.value"

REQUIRED = frozenset({
    COLOR_NAME, FINISH_NAME, PORT_NAME, PORT_REGION_NAME, PRODUCT_COLLECTION_CITY, PRODUCT_LOT_CODE,
    PRODUCT_ORIGIN_CITY, PRODUCT_ORIGIN_COUNTY, PRODUCT_TITLE, PRODUCT_CATEGORY_NAME, QUALITY_NAME, TYPE_NAME,
    VARIATION_NAME, VARIATION_ALIAS_VALUE,
})
NAME, IDENTIFIER = "name", "identifier"
# the attribute vocabularies (review cards, the attribute-id route) and the field each one's value is
ATTRIBUTE_FIELDS = {"color": COLOR_NAME, "finish": FINISH_NAME, "type": TYPE_NAME, "quality": QUALITY_NAME}


class FieldKindsError(RuntimeError):
    """The published map is absent or invalid: nothing that writes a name may run on a guessed kind."""


# (sha256 of the file's bytes, its validated fields). Only a VALID map is cached: a failure is re-read on every call,
# so a map that arrives after boot is used at once, with no restart.
_cache: tuple[str, dict[str, str]] | None = None


def _path() -> Path:
    return Path(SETTINGS.paths.field_kinds_json)


def parse(raw: bytes, where) -> dict[str, str]:
    """The validated `fields` of a map file's bytes; raises FieldKindsError naming `where` and the reason."""
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FieldKindsError(f"{where}: not valid JSON ({exc})") from exc
    if not isinstance(doc, dict) or doc.get("brand") != BRAND:
        found = doc.get("brand") if isinstance(doc, dict) else None
        raise FieldKindsError(f"{where}: brand is {found!r}, expected {BRAND!r}")
    fields = doc.get("fields")
    if not isinstance(fields, dict):
        raise FieldKindsError(f"{where}: no fields object")
    missing = sorted(REQUIRED - fields.keys())
    if missing:
        raise FieldKindsError(f"{where}: missing fields {missing}")
    unknown = sorted(f"{k}={v!r}" for k, v in fields.items() if v not in (NAME, IDENTIFIER))
    if unknown:
        raise FieldKindsError(f"{where}: unknown kind {unknown}")
    return dict(fields)


def kinds() -> dict[str, str]:
    """The current map, re-read when the file changes (keyed by its hash)."""
    global _cache
    path = _path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise FieldKindsError(f"{path}: absent (field-kinds.json is not published)") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if _cache is None or _cache[0] != digest:
        _cache = (digest, parse(raw, path))
    return _cache[1]


def kind(field: str) -> str:
    if field not in REQUIRED:
        raise KeyError(f"unknown name field {field!r}")
    return kinds()[field]


def clean(field: str, text: str) -> str:
    """`text` cleaned by its field's kind; raises UnfoldableName for a letter with no ASCII form."""
    return clean_name(text) if kind(field) == NAME else clean_identifier(text)


def display(field: str, text: str) -> str:
    """`text` as the scraper writes it: a name is cleaned and title-cased, an identifier cleaned with its case kept."""
    return title_case(clean_name(text)) if kind(field) == NAME else clean_identifier(text)
