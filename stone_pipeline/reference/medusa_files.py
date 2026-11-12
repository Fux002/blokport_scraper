"""The one reader of each Medusa download in from_medusa/<env>/ (VE2 design, section 1).

Every consumer reads a file through here, so the columns, the blank-row rules and the guards live in one place.
Columns are found by header name: their order is free and an unknown column is ignored, but a missing required
column fails loud (a renamed header would otherwise read as an empty vocabulary).

Imports only the stdlib and core.text: config.settings reads attributes.csv at import time (the category pcats),
so this module must never import settings or the loaders (an import cycle).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from stone_pipeline.core.text import match_key

ATTRIBUTE_COLUMNS = ("category", "value", "sourceid")
_STATUS_COLUMNS = ("status", "internal")
# Table 569: on a `type` row, the product_category handles (D11) the type has a valid combination in, '|'-joined
# ('' when it has none); empty on every other row. load_attributes scopes the type vocabulary to the pack with it.
CATEGORY_HANDLES_COLUMN = "category_handles"
_STATUS = ("active", "archived")
_INTERNAL = {"true": True, "false": False}
# Key, Name and Type are the variation's identity (phase 1: a Key names one stone of one type, and Type is its
# canonical type NAME), so they are required. Id and Aliases are read as empty when their column is absent: the
# scraper's base files (variants_export_base*.csv) carry no Id. Image and Volume are gone (SC-4 2b): a variety's
# icons are per (Key, category) pairs on S3, and the volume was never the scraper's.
EXPORT_COLUMNS = ("Key", "Name", "Type")
# The ONE column list of every variety file the scraper writes: the three published files (1_variants_full,
# 1_variants_update, the base) and the curate delta. The backend's export is this plus Id, status and owner.
VARIETY_FILE_COLUMNS = ["Key", "Name", "Type", "Aliases"]


@dataclass(frozen=True)
class AttributeValue:
    """One attributes.csv row: a vocabulary value and its Medusa id (sourceid may be empty)."""

    category: str
    value: str
    sourceid: str
    status: str        # "active" | "archived"
    internal: bool
    # the type's product_category handles (table 569), sorted; None when the file has no category_handles column
    category_handles: tuple[str, ...] | None = None

    @property
    def assignable(self) -> bool:
        """New data may map to this value: active and not internal."""
        return self.status == "active" and not self.internal


def read_attributes(src: Path | str, *, require_status: bool,
                    require_category_handles: bool = False) -> list[AttributeValue]:
    """attributes.csv -> its values, in file order. `src` is the file's Path, or a str holding the file's TEXT
    (a str is never taken as a path). A row without category or value is skipped; an empty sourceid is kept
    (each caller decides what an id-less value means). Without a status/internal column every value is active
    and not internal, unless `require_status`, which makes the absent column an error; `require_category_handles`
    does the same for category_handles (table 569: the fetch's check and the type vocabulary require it). A
    conflicting duplicate raises: two values of one category that match_key to the same name with different ids,
    across all statuses, since the value->id map would otherwise silently keep the last one."""
    label = str(src) if isinstance(src, Path) else "attributes.csv"
    out: list[AttributeValue] = []
    ids: dict[tuple[str, str], AttributeValue] = {}
    required = (ATTRIBUTE_COLUMNS + (_STATUS_COLUMNS if require_status else ())
                + ((CATEGORY_HANDLES_COLUMN,) if require_category_handles else ()))
    for line, row, fields in _rows(src, label, required):
        category = (row.get("category") or "").strip()
        value = (row.get("value") or "").strip()
        if not category or not value:
            continue
        item = AttributeValue(category, value, (row.get("sourceid") or "").strip(),
                              _status(row, fields, label, line), _internal(row, fields, label, line),
                              _category_handles(row, fields))
        _guard_duplicate(ids, item, label)
        out.append(item)
    return out


@dataclass(frozen=True)
class ExportVariation:
    """One variants export row: Medusa's export, or a scraper-owned file of the same shape (the Id-free base)."""

    key: str
    name: str
    id: str                   # empty in the Id-free base and for a variation Medusa has not minted yet
    type: str                 # the canonical stone-type NAME (Medusa's attribute value), never the Key's slug
    aliases: tuple[str, ...]
    status: str               # "active" | "archived"

    @property
    def archived(self) -> bool:
        return self.status == "archived"


def read_variants_export(src: Path | str, *, require_status: bool,
                         name: str = "variants_export.csv") -> list[ExportVariation]:
    """A variants export -> its rows, in file order. `src` is the file's Path, or a str holding the file's TEXT
    (a str is never taken as a path; `name` is the file an error names then, since the text form also reads a base). Cells are stripped and Aliases is split on '|' into its non-empty names.
    Only a blank row (every cell empty) is skipped: what a row without Key, Name or Id means differs per caller
    (the base has no Id; tree_build counts a half row as malformed), so each caller keeps its own rule. Without
    a status column every row is active, unless `require_status`, which makes the absent column an error."""
    label = str(src) if isinstance(src, Path) else name
    required = EXPORT_COLUMNS + (("status",) if require_status else ())
    out: list[ExportVariation] = []
    for line, row, fields in _rows(src, label, required):
        cell = {name: (row.get(name) or "").strip() for name in fields}
        if not any(cell.values()):
            continue
        out.append(ExportVariation(
            cell["Key"], cell["Name"], cell.get("Id", ""), cell["Type"],
            tuple(a.strip() for a in cell.get("Aliases", "").split("|") if a.strip()),
            _status(row, fields, label, line)))
    return out


def append_attributes(path: Path, values: Iterable[AttributeValue]) -> None:
    """Append values to an existing attributes.csv in ITS header's column order, so a file with status and
    internal columns (or any extra one, left empty) stays well-formed for read_attributes."""
    with path.open(encoding="utf-8-sig", newline="") as h:
        header = [name.strip() for name in next(csv.reader(h), [])]
    missing = [c for c in ATTRIBUTE_COLUMNS if c not in header]
    if missing:
        raise ValueError(f"{path}: missing required column(s) {', '.join(missing)}")
    with path.open("a", newline="", encoding="utf-8") as h:
        writer = csv.writer(h)
        for v in values:
            cells = {"category": v.category, "value": v.value, "sourceid": v.sourceid,
                     "status": v.status, "internal": "true" if v.internal else "false"}
            writer.writerow([cells.get(name, "") for name in header])


def _rows(src: Path | str, label: str, required: tuple[str, ...]) -> Iterator[tuple[int, dict, set[str]]]:
    """(line number, row by stripped header name, the header's names) for each data row; raises when a required
    column is missing. A Path is opened as utf-8-sig; text drops a leading BOM the same way."""
    if isinstance(src, Path):
        with src.open(encoding="utf-8-sig", newline="") as h:
            yield from _parse(h, label, required)
    else:
        yield from _parse(io.StringIO(src.removeprefix("\ufeff"), newline=""), label, required)


def _parse(handle, label: str, required: tuple[str, ...]) -> Iterator[tuple[int, dict, set[str]]]:
    reader = csv.DictReader(handle)
    reader.fieldnames = [name.strip() for name in reader.fieldnames or []]
    fields = set(reader.fieldnames)
    missing = [c for c in required if c not in fields]
    if missing:
        raise ValueError(f"{label}: missing required column(s) {', '.join(missing)}")
    for row in reader:
        yield reader.line_num, row, fields


def _category_handles(row: dict, fields: set[str]) -> tuple[str, ...] | None:
    if CATEGORY_HANDLES_COLUMN not in fields:
        return None
    return tuple(sorted({h.strip() for h in (row.get(CATEGORY_HANDLES_COLUMN) or "").split("|") if h.strip()}))


def _status(row: dict, fields: set[str], label: str, line: int) -> str:
    if "status" not in fields:
        return "active"
    cell = (row.get("status") or "").strip().lower()
    if cell not in _STATUS:
        raise ValueError(f"{label} line {line}: status {row.get('status')!r} is not one of {', '.join(_STATUS)}")
    return cell


def _internal(row: dict, fields: set[str], label: str, line: int) -> bool:
    if "internal" not in fields:
        return False
    cell = (row.get("internal") or "").strip().lower()
    if cell not in _INTERNAL:
        raise ValueError(f"{label} line {line}: internal {row.get('internal')!r} is not true or false")
    return _INTERNAL[cell]


def _guard_duplicate(ids: dict[tuple[str, str], AttributeValue], item: AttributeValue, label: str) -> None:
    """An id-less row carries no id to conflict with, so only rows with a sourceid take part."""
    if not item.sourceid:
        return
    key = (match_key(item.category), match_key(item.value))
    seen = ids.setdefault(key, item)
    if seen.sourceid != item.sourceid:
        raise ValueError(
            f"{label}: {item.category} values {seen.value!r} and {item.value!r} normalize to the same key "
            f"{key[1]!r} but map to different ids ({seen.sourceid} vs {item.sourceid}); attribute names must be "
            f"unique per category so the value->id mapping is unambiguous")
