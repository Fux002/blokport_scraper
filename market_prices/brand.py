"""The brand separation of the competitor-price path, in one place: one infrastructure, every brand's data apart.

A run is for ONE brand, its run brand. Every per-brand file (the site register, the sheet layouts, the scope
vocabulary) is read only through load_brand_document, which opens `<directory>/<run brand>.yaml` and nothing else, and
refuses a file that declares another brand.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

SLUG = re.compile(r"[a-z0-9_]+")

Document = TypeVar("Document", bound=BaseModel)


def load_brand_document(directory: Path, run_brand: str, model: type[Document], error: type[Exception],
                        what: str) -> Document:
    """The run brand's own file in `directory`, validated as `model`. Any problem raises `error`, so nothing runs on a
    half-read file: a brand that is not a slug, an absent file, invalid YAML, a document that does not validate, or a
    file declaring another brand. `what` names the file kind in the message."""
    if not SLUG.fullmatch(run_brand):
        raise error(f"brand {run_brand!r} must match {SLUG.pattern}")
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
