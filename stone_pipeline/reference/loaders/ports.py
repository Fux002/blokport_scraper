"""ports.csv: the environment's Medusa port ids. Port ids differ per environment, so production resolves
ONLY against its own downloaded file and never the dev-id fallback."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.reference.loaders.common import env, log, norm


@dataclass
class Ports:
    by_country: dict[str, list[str]] = field(default_factory=dict)  # iso2 -> [port_id]
    iso_by_port: dict[str, str] = field(default_factory=dict)        # port_id -> iso2
    by_name: dict[str, str] = field(default_factory=dict)            # norm(name) -> port_id
    by_locode: dict[str, str] = field(default_factory=dict)          # UN/LOCODE -> port_id

    def country_of(self, port_id: str | None) -> str | None:
        return self.iso_by_port.get((port_id or "").strip())

    def resolve(self, token: str) -> str | None:
        """Resolve a sources.yaml port entry to a port id: a known id passes through,
        else a UN/LOCODE ('ITBDS') or a port name ('Brindisi') from ports.csv."""
        t = (token or "").strip()
        if t in self.iso_by_port:
            return t
        return self.by_locode.get(t.upper()) or self.by_name.get(norm(t))

    def all_ids(self) -> list[str]:
        return [pid for lst in self.by_country.values() for pid in lst]


def load_ports(path: Path | None = None) -> Ports:
    """Load the env's Medusa port ids from from_medusa/<env>/ports.csv. Port ids differ per env, so
    PRODUCTION resolves ONLY against its own downloaded file and REFUSES the dev-id catalog_source
    fallback: shipping dev ids into prod makes Medusa silently drop every port (all port_of_origin
    blank). Dev may fall back to the committed catalog_source master. An explicit `path` (tests)
    is honored verbatim, env-independent."""
    settings = env().SETTINGS
    if path is not None:
        candidate = Path(path)
    else:
        candidate = settings.paths.ports_csv
        if not candidate.exists():
            if env().IS_PRODUCTION:
                # No prod ports export in place yet. Return empty so ports resolve to nothing and every
                # row is flagged port_unresolved (visible in review), instead of SILENTLY loading dev
                # ids. Self-heals once from_medusa/production/ports.csv is fetched. Never fall back to
                # the dev-id master in prod -- same invariant as the prod bucket/owner-id guards.
                log.error("production ports.csv missing (%s): ports UNRESOLVED until the prod Medusa "
                          "ports export is placed there -- refusing the dev-id fallback",
                          settings.paths.ports_csv)
                return Ports()
            candidate = settings.paths.ports_csv_fallback
    ports = Ports()
    if not candidate.exists():
        log.warning("ports.csv absent; origin->ports resolution will fall back to default")
        return ports
    with candidate.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            pid = (record.get("port_id") or record.get("id") or record.get("Id") or "").strip()
            if not pid:
                continue
            iso = (record.get("country_iso") or "").strip().upper()
            if iso:
                ports.by_country.setdefault(iso, []).append(pid)
                ports.iso_by_port[pid] = iso
            if name := (record.get("name") or "").strip():       # for resolving by name
                ports.by_name[norm(name)] = pid
            if locode := (record.get("un_locode") or "").strip().upper():
                ports.by_locode[locode] = pid
    return ports
