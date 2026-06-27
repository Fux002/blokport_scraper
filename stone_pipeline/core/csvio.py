"""Crash-safe + spreadsheet-safe CSV writing.

Transitional: SYNC_LEDGER_DESIGN.md replaces the CSV round-trip with a ledger, but while these files
exist they must be (a) written atomically so a crash never leaves a truncated file that the next run
reads as a baseline (the combination-staleness chain), and (b) free of spreadsheet formula-injection
for the files an operator opens in Excel. Both concerns disappear once the ledger lands; until then,
every delivered CSV goes through here.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Callable, Iterable, Sequence

# a cell starting with one of these executes / is interpreted as a formula when opened in Excel
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    """Neutralise CSV formula injection: prefix a leading =,+,-,@ (or tab/CR) cell with a single
    quote so a scraped value like '=HYPERLINK(...)' is treated as text, not executed."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in _FORMULA_LEADERS else s


def atomic_write(path: Path | str, write_fn: Callable) -> None:
    """Write through a temp file then os.replace, so `path` is never a partial/truncated file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", newline="", encoding="utf-8") as handle:
            write_fn(handle)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def write_rows(path: Path | str, header: Sequence[str], rows: Iterable[Sequence]) -> int:
    """Atomically write raw sequence rows (e.g. combination tuples). No sanitising -- callers that
    emit only ids/keys use this; it avoids the per-cell cost on the multi-million-row files."""
    rows = list(rows)
    atomic_write(path, lambda h: (lambda w: (w.writerow(header), w.writerows(rows)))(csv.writer(h)))
    return len(rows)


def write_dicts(path: Path | str, fieldnames: Sequence[str], rows: Iterable[dict],
                sanitize: bool = False) -> int:
    """Atomically write dict rows. Column-strict: writes EXACTLY `fieldnames`, projecting each row to
    them -- so an extra internal key on a row (e.g. '_status'/'_added') is dropped, never crashes the
    writer. sanitize=True additionally escapes formula injection in every cell -- use it for any file
    carrying SCRAPED text (variant Name/Aliases, product Title/Description) that an operator may open
    in Excel."""
    rows = list(rows)
    fields = list(fieldnames)
    cell = safe_cell if sanitize else (lambda v: "" if v is None else v)

    def _w(handle):
        w = csv.DictWriter(handle, fieldnames=fields)
        w.writeheader()
        w.writerows({k: cell(r.get(k, "")) for k in fields} for r in rows)

    atomic_write(path, _w)
    return len(rows)
