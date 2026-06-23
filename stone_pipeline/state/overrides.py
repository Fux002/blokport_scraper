"""Inbound manual overrides (section 8.4).

state/manual_overrides.csv is keyed by (src_site, surrogate_key) and carries
human-set field values. The override is the highest-priority strategy for every
resolver, so a manual value always wins and is the clean way to inject any
missing element (a bundle size, an origin, a colour, a variation) without editing
the scrape. Overrides are versioned by the reference snapshot and logged.

CSV shape: src_site, surrogate_key, field, value
where field is a canonical field name the resolver fills (e.g. color_name,
variation_id, bundle_size, origin_country_code, title).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field as dc_field
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS


@dataclass
class Overrides:
    # (src_site, surrogate_key) -> {field: value}
    by_key: dict[tuple[str, str], dict[str, str]] = dc_field(default_factory=dict)

    def get(self, src_site: str, surrogate_key: str, field: str):
        return self.by_key.get((src_site, surrogate_key), {}).get(field)

    def has_any(self, src_site: str, surrogate_key: str) -> bool:
        return bool(self.by_key.get((src_site, surrogate_key)))

    def __len__(self) -> int:
        return sum(len(v) for v in self.by_key.values())


def load_overrides(path: Path | None = None) -> Overrides:
    path = Path(path or SETTINGS.paths.overrides_csv)
    overrides = Overrides()
    if not path.exists():
        return overrides
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            site = (record.get("src_site") or "").strip()
            key = (record.get("surrogate_key") or "").strip()
            field = (record.get("field") or "").strip()
            value = (record.get("value") or "").strip()
            if site and key and field:
                overrides.by_key.setdefault((site, key), {})[field] = value
    return overrides
