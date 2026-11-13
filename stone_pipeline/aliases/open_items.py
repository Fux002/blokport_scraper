"""The cleanup's open review items (P2-10, PHASE-2-SPEC WP2.3 "Review API"; agreed with Scraper C6, 11-12): ONE format,
written by the cleanup (aliases.cleanup) and served read-only by GET /config/v1/review/aliases.

review/<env>/alias_cleanup_open.csv, written on every cleanup run (the header only when there is nothing open):
- columns kind,Key,Value,reason; kind is one of OPEN_KINDS:
  - digit_non_code: a served alias holding a digit that the code pattern does not accept (reason DIGIT_REASON);
  - admin_refused: an admin alias (the export's AdminAliases) failing the boundary (reason: the alias_problem code);
- UTF-8 with no BOM, \\n line ends, minimal quoting, sorted by (kind, Key, alias_key(Value), Value).
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Iterable, Optional

from stone_pipeline.core.text import alias_key

OPEN_FILE = "alias_cleanup_open.csv"
OPEN_COLUMNS = ("kind", "Key", "Value", "reason")
OPEN_KINDS = ("digit_non_code", "admin_refused")
DIGIT_REASON = "digit_not_code"


class OpenItemsUnreadable(ValueError):
    """The file exists but is not this format (its header): named, never read as "nothing open"."""


def open_items_bytes(rows: Iterable[tuple[str, str, str, str]]) -> bytes:
    """The file from (kind, Key, Value, reason) rows, in the one order. An unknown kind raises."""
    rows = list(rows)
    if unknown := sorted({r[0] for r in rows} - set(OPEN_KINDS)):
        raise ValueError(f"unknown open item kind(s) {unknown}; known: {list(OPEN_KINDS)}")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(OPEN_COLUMNS)
    writer.writerows(sorted(rows, key=lambda r: (r[0], r[1], alias_key(r[2]), r[2])))
    return out.getvalue().encode("utf-8")


def read_open_items(path: Path) -> Optional[list[dict]]:
    """[{kind, key, value, reason}] in file order, or None when the file is absent. A header that is not OPEN_COLUMNS
    raises OpenItemsUnreadable naming the file."""
    path = Path(path)
    if not path.exists():
        return None
    reader = csv.reader(io.StringIO(path.read_text(encoding="utf-8")))
    header = next(reader, None)
    if tuple(header or ()) != OPEN_COLUMNS:
        raise OpenItemsUnreadable(f"{path.name}: header {header} is not {list(OPEN_COLUMNS)}")
    return [{"kind": kind, "key": key, "value": value, "reason": reason} for kind, key, value, reason in reader]
