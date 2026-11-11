"""An uploaded price sheet read into plain tables, within caps. Only `.csv` (UTF-8) and `.xlsx` are read; an xlsx is
size-checked as a zip before it is parsed, parsed through defusedxml, and its formulas are never evaluated: a formula
cell is its cached value, and one saved without a value is marked so its row can be refused."""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Optional
from xml.etree.ElementTree import ParseError

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

from market_prices import config

_WORKBOOK_PART = "xl/workbook.xml"


class FileRefused(Exception):
    """This file cannot be read as any layout; it is reported and left in the intake, never half-read."""


@dataclass(frozen=True)
class Table:
    name: Optional[str]                                 # the worksheet's name; None for a csv
    rows: list[list[object]]
    formula_gaps: frozenset[tuple[int, int]] = field(default_factory=frozenset)   # 0-based (row, column)


def read_tables(name: str, data: bytes) -> list[Table]:
    """The file's tables: one for a csv, one per worksheet for an xlsx."""
    if len(data) > config.MAX_INTAKE_FILE_BYTES:
        raise FileRefused(f"{name}: {len(data)} bytes is over the cap of {config.MAX_INTAKE_FILE_BYTES} bytes")
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".csv":
        return [_csv_table(name, data)]
    if suffix == ".xlsx":
        return _xlsx_tables(name, data)
    raise FileRefused(f"{name}: only .csv and .xlsx are read (save the sheet as .xlsx)")


def _csv_table(name: str, data: bytes) -> Table:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FileRefused(f"{name}: not UTF-8 ({exc.reason} at byte {exc.start})") from exc
    rows: list[list[object]] = []
    try:
        for row in csv.reader(io.StringIO(text, newline="")):
            _check_grid(name, len(rows) + 1, len(row))
            rows.append(list(row))
    except csv.Error as exc:
        raise FileRefused(f"{name}: not a readable csv: {exc}") from exc
    return Table(None, rows)


def _xlsx_tables(name: str, data: bytes) -> list[Table]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = archive.infolist()
    except zipfile.BadZipFile as exc:
        raise FileRefused(f"{name}: not an xlsx workbook ({exc})") from exc
    if _WORKBOOK_PART not in {part.filename for part in parts}:
        raise FileRefused(f"{name}: not an xlsx workbook (no {_WORKBOOK_PART})")
    expanded = sum(part.file_size for part in parts)
    if expanded > config.MAX_XLSX_UNCOMPRESSED_BYTES:
        raise FileRefused(f"{name}: expands to {expanded} bytes, over the cap of "
                          f"{config.MAX_XLSX_UNCOMPRESSED_BYTES} bytes")
    try:
        values = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        formulas = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            return [_sheet_table(name, values[title], formulas[title]) for title in values.sheetnames]
        finally:
            values.close()
            formulas.close()
    except (InvalidFileException, KeyError, ParseError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        raise FileRefused(f"{name}: not a readable xlsx workbook ({type(exc).__name__}: {exc})") from exc


def _sheet_table(name: str, values, formulas) -> Table:
    """One worksheet, its rows from row 1 (a recorded dimension is not trusted), with each formula cell that has no
    cached value marked."""
    values.reset_dimensions()
    formulas.reset_dimensions()
    rows: list[list[object]] = []
    gaps: set[tuple[int, int]] = set()
    pairs = zip(values.iter_rows(min_row=1, values_only=True), formulas.iter_rows(min_row=1, values_only=True))
    for index, (cached, written) in enumerate(pairs):
        _check_grid(f"{name} sheet {values.title!r}", index + 1, len(cached))
        rows.append(list(cached))
        gaps.update((index, column) for column, cell in enumerate(written)
                    if isinstance(cell, str) and cell.startswith("=")
                    and (column >= len(cached) or cached[column] is None))
    return Table(values.title, rows, frozenset(gaps))


def _check_grid(where: str, rows: int, columns: int) -> None:
    if rows > config.MAX_SHEET_ROWS:
        raise FileRefused(f"{where}: more than {config.MAX_SHEET_ROWS} rows")
    if columns > config.MAX_SHEET_COLUMNS:
        raise FileRefused(f"{where}: row {rows} has {columns} columns, over the cap of {config.MAX_SHEET_COLUMNS}")
