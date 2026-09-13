"""units.csv: unit tokens -> (dimension, canonical unit, factor)."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from stone_pipeline.core.numbers import parse_number
from stone_pipeline.reference.loaders.common import env, log


@dataclass
class UnitEntry:
    dimension: str
    canonical: str
    factor: float


@dataclass
class Units:
    by_token: dict[str, UnitEntry] = field(default_factory=dict)

    def convert(self, value: float, token: str, expect: str | None = None) -> Optional[float]:
        """Scale `value` by the token's factor. When `expect` is given (e.g. "weight"), a token of a
        DIFFERENT dimension returns None instead of a silently-wrong scaling -- so a length/area token
        that leaked into a weight field is rejected rather than shipped as a plausible-magnitude kg.
        The `dimension` column carries this; without `expect` the behaviour is unchanged (dimension ignored)."""
        entry = self.by_token.get((token or "").strip().casefold())
        if entry is None:
            return None
        if expect is not None and entry.dimension != expect:
            return None
        return value * entry.factor


def load_units(path: Path | None = None) -> Units:
    path = Path(path or env().SETTINGS.paths.units_csv)
    units = Units()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            token = (record.get("token") or "").strip().casefold()
            if not token:
                continue
            # empty factor is a legit same-unit token (-> 1.0); a PRESENT but non-numeric factor is bad data
            # -- skip it loudly rather than crash the whole reference load or silently mis-scale to 1.0.
            factor_raw = (record.get("factor") or "").strip()
            factor = 1.0 if not factor_raw else parse_number(factor_raw)
            if factor is None:
                log.warning("units: skipping token with a non-numeric factor",
                            extra={"extra_fields": {"token": token, "factor": factor_raw}})
                continue
            units.by_token[token] = UnitEntry(
                dimension=(record.get("dimension") or "").strip(),
                canonical=(record.get("canonical") or "").strip(),
                factor=factor,
            )
    return units
