"""Inbound manual overrides (section 8.4).

state/manual_overrides.csv is keyed by (src_site, surrogate_key) and carries
human-set field values. The override is the highest-priority strategy for every
resolver, so a manual value always wins and is the clean way to inject any
missing element (a bundle size, an origin, a colour, a variation, a size) without
editing the scrape. Overrides are versioned by the reference snapshot and logged.

CSV shape: src_site, surrogate_key, field, value, confirmed_by, confirmed_at, source
where field is a canonical field name the resolver fills (e.g. color_name,
variation_id, bundle_size, origin_country_code, title, length, width, height).

Every entry names who confirmed it, when (an ISO-8601 date or time), and from what
(a vendor URL, a card, the office): an override is an exception to the scrape, and an
exception that does not name its remover is refused (EXCEPTION GRADES, grade 2). So a
file without those columns, or an entry with any of them blank, or a blank value (an
override sets or updates a field, it never clears one), fails the load, naming the
line. A missing file is no overrides.

The file's authority is S3 (io.medusa_inputs.fetch_overrides fetches it fresh on every
produce); an operator edits it there, never on a task.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS

_KEY_COLUMNS = ("src_site", "surrogate_key", "field", "value")
_PROVENANCE_COLUMNS = ("confirmed_by", "confirmed_at", "source")


class OverridesError(ValueError):
    """The overrides file cannot be used: a missing column, or an entry the load refuses (named by its line)."""


@dataclass(frozen=True)
class Provenance:
    """Who confirmed an override, when, and from what."""
    confirmed_by: str
    confirmed_at: str
    source: str


@dataclass
class Overrides:
    # (src_site, surrogate_key) -> {field: value}
    by_key: dict[tuple[str, str], dict[str, str]] = dc_field(default_factory=dict)
    # (src_site, surrogate_key, field) -> who confirmed it, when and from what (filled by load_overrides)
    provenance: dict[tuple[str, str, str], Provenance] = dc_field(default_factory=dict)

    def get(self, src_site: str, surrogate_key: str, field: str):
        return self.by_key.get((src_site, surrogate_key), {}).get(field)

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_key.values())


def load_overrides(path: Path | None = None) -> Overrides:
    """The overrides file, every entry validated (the module docstring). A missing file is no overrides; an
    unreadable one raises (OSError), a malformed one raises OverridesError naming the line."""
    path = Path(path or SETTINGS.paths.overrides_csv)
    overrides = Overrides()
    if not path.exists():
        return overrides
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in (*_KEY_COLUMNS, *_PROVENANCE_COLUMNS) if c not in (reader.fieldnames or ())]
        if missing:
            raise OverridesError(f"{path.name} lacks the column(s) {', '.join(missing)}")
        for record in reader:
            cells = {c: (record.get(c) or "").strip() for c in (*_KEY_COLUMNS, *_PROVENANCE_COLUMNS)}
            where = (f"{path.name} line {reader.line_num} "
                     f"({cells['src_site']} {cells['surrogate_key']} {cells['field']})")
            blank = [c for c in (*_KEY_COLUMNS, *_PROVENANCE_COLUMNS) if not cells[c]]
            if blank:
                raise OverridesError(f"{where}: {', '.join(blank)} is blank")
            try:
                datetime.fromisoformat(cells["confirmed_at"])
            except ValueError as exc:
                raise OverridesError(f"{where}: confirmed_at {cells['confirmed_at']!r} is not an ISO-8601 date") \
                    from exc
            site, key, field = cells["src_site"], cells["surrogate_key"], cells["field"]
            overrides.by_key.setdefault((site, key), {})[field] = cells["value"]
            overrides.provenance[(site, key, field)] = Provenance(*(cells[c] for c in _PROVENANCE_COLUMNS))
    return overrides
