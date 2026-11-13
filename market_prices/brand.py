"""The brand separation of the competitor-price path, in one place: one infrastructure, every brand's data apart.

A run is for ONE brand, its run brand. The rules, each named:
- brand_required: every register row, observation, site_status row and manifest carries the run's brand; a record
  with an empty or another brand is refused (require_run_brand; the rows' own `brand` must be a slug).
- one_register_per_site: register.check_one_register_per_site.
- brand_vocabulary_isolated: every per-brand file (the site register, the sheet layouts, the scope vocabulary) is read
  only through load_brand_document, which opens `<directory>/<run brand>.yaml` and nothing else, and refuses a file
  that declares another brand.
- brand_scoped_output: every key the path reads or writes is built by output_key under `<env>/market_prices/<run
  brand>/`, and require_brand_key refuses any other.
- shared_code_brand_free: no brand name in this package's code (tests/test_market_prices_brand.py); a brand's facts
  live only in its own files.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Protocol, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from market_prices import config
from stone_pipeline.config.settings import ENV_SEGMENT

SLUG = re.compile(r"[a-z0-9_]+")

Document = TypeVar("Document", bound=BaseModel)


class BrandError(Exception):
    """A record, file or key is not the run brand's own. Nothing of another brand is read or written."""


class Branded(Protocol):
    brand: str


def _require_slug(run_brand: str, error: type[Exception] = BrandError) -> None:
    if not SLUG.fullmatch(run_brand):
        raise error(f"brand {run_brand!r} must match {SLUG.pattern}")


def require_run_brand(run_brand: str, records: Iterable[Branded], what: str) -> None:
    """brand_required: every record is the run brand's; any other brand, the empty one included, refuses them all."""
    if foreign := sorted({record.brand for record in records if record.brand != run_brand}):
        raise BrandError(f"{what} of brand(s) {foreign} in a {run_brand!r} run")


def output_key(run_brand: str, *parts: str) -> str:
    """brand_scoped_output: the S3 key `<env>/market_prices/<run brand>/<parts>`. A part that is empty, holds a '/' or
    starts with '.' (so '..' too) is refused, so a key built here never leaves the brand's prefix."""
    _require_slug(run_brand)
    for part in parts:
        if not part or "/" in part or part.startswith("."):
            raise BrandError(f"key part {part!r} could leave the {run_brand} prefix")
    return "/".join((config.S3_BRAND_PREFIX.format(env_segment=ENV_SEGMENT, brand=run_brand), *parts))


def require_brand_key(run_brand: str, key: str) -> None:
    """brand_scoped_output: `key` is under the run brand's own prefix, with no empty, '.' or '..' segment below it."""
    prefix = output_key(run_brand) + "/"
    if not key.startswith(prefix) or any(segment in ("", ".", "..") for segment in key[len(prefix):].split("/")):
        raise BrandError(f"key {key!r} is outside the {run_brand} prefix {prefix}")


def load_brand_document(directory: Path, run_brand: str, model: type[Document], error: type[Exception],
                        what: str) -> Document:
    """The run brand's own file in `directory`, validated as `model`. Any problem raises `error`, so nothing runs on a
    half-read file: a brand that is not a slug, an absent file, invalid YAML, a document that does not validate, or a
    file declaring another brand. `what` names the file kind in the message."""
    _require_slug(run_brand, error)
    path = directory / f"{run_brand}.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise error(f"no {what} for brand {run_brand!r} at {path}") from exc
    except yaml.YAMLError as exc:
        raise error(f"{path} is not valid YAML: {exc}") from exc
    try:
        loaded = model.model_validate(document)
    except ValidationError as exc:
        raise error(f"{path} does not validate: {exc}") from exc
    if loaded.brand != run_brand:
        raise error(f"{path} declares brand {loaded.brand!r}")
    return loaded
