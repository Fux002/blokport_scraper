"""Source contracts and baselines (section 6, section 7 Stage 0).

A contract declares what a source's scrape file should look like: required and
optional columns, per-field fill floors, value patterns, and the adapter
version. The baseline holds the last-good per-source metrics (row count and
per-field fill rate) and is auto-updated on healthy runs only (section 7), so a
degraded scrape never poisons the reference point.

The contract can be generated from a sample (section 7A.1, 7A.3 step 3), keeping
the health contract and the adapter in sync by construction.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import polars as pl
import yaml

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import csvio


# Columns every ScraperBase subclass emits (scrapers/base.py BASE_COLUMNS). They
# are always present and source-independent, so the health check must treat them
# as known rather than flagging them as unexpected drift.
FRAMEWORK_COLUMNS = ["format", "image_count", "image_urls", "image_filenames_local",
                     "scrape_timestamp", "raw_json"]


@dataclass
class SourceContract:
    source: str
    adapter_version: str = "0.0.0"
    required_columns: list[str] = field(default_factory=list)
    optional_columns: list[str] = field(default_factory=list)
    # per-field minimum non-empty fraction expected (only required fields usually)
    fill_floors: dict[str, float] = field(default_factory=dict)
    # field -> list of allowed values (closed set check); empty means no check
    value_sets: dict[str, list[str]] = field(default_factory=dict)
    # field -> regex pattern string for a shape check
    value_patterns: dict[str, str] = field(default_factory=dict)
    row_baseline: int = 0

    @property
    def all_columns(self) -> list[str]:
        return [*self.required_columns, *self.optional_columns, *FRAMEWORK_COLUMNS]


def load_contracts(path: Path | None = None) -> dict[str, SourceContract]:
    path = Path(path or SETTINGS.paths.source_contracts_yaml)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    contracts: dict[str, SourceContract] = {}
    for source, body in data.items():
        body = body or {}
        contracts[source] = SourceContract(source=source, **body)
    return contracts


def load_contract(source: str, path: Path | None = None) -> Optional[SourceContract]:
    return load_contracts(path).get(source)


def generate_contract_from_sample(
    source: str,
    frame: pl.DataFrame,
    required_columns: list[str],
    adapter_version: str = "1.0.0",
    fill_floor: float = 0.8,
) -> SourceContract:
    """Build an initial contract from a sample (the framework does this during
    onboarding). Required columns are supplied by the adapter author; everything
    else is optional. Fill floors seed from the observed fill of required fields,
    clamped so a slightly emptier real scrape does not falsely fail."""
    observed = column_fill_rates(frame)
    optional = [c for c in frame.columns if c not in required_columns]
    floors = {
        col: max(0.0, round(min(fill_floor, observed.get(col, 0.0)) - 0.1, 3))
        for col in required_columns
    }
    return SourceContract(
        source=source,
        adapter_version=adapter_version,
        required_columns=list(required_columns),
        optional_columns=optional,
        fill_floors=floors,
        row_baseline=frame.height,
    )


def write_contract(contract: SourceContract, path: Path | None = None) -> Path:
    path = Path(path or SETTINGS.paths.source_contracts_yaml)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    body = asdict(contract)
    body.pop("source")
    existing[contract.source] = body
    # atomic: a crash mid-write must not leave a truncated YAML the next run reads as a contract.
    csvio.atomic_write(path, lambda h: h.write(yaml.safe_dump(existing, sort_keys=False)))
    return path


# --- fill-rate helpers (shared by contract generation and the health gate) ----
def column_fill_rates(frame: pl.DataFrame) -> dict[str, float]:
    """Per-column non-empty fraction. Empty means null or blank/whitespace."""
    if frame.height == 0:
        return {c: 0.0 for c in frame.columns}
    rates: dict[str, float] = {}
    height = frame.height
    for col in frame.columns:
        series = frame.get_column(col).cast(pl.String, strict=False)
        non_empty = series.fill_null("").str.strip_chars().str.len_chars() > 0
        rates[col] = round(int(non_empty.sum()) / height, 4)
    return rates


# --- baselines (state/scrape_baselines.json) ----------------------------------
@dataclass
class Baseline:
    source: str
    row_count: int = 0
    fill_rates: dict[str, float] = field(default_factory=dict)
    adapter_version: str = ""
    updated_at: str = ""


def load_baselines(path: Path | None = None) -> dict[str, Baseline]:
    path = Path(path or SETTINGS.paths.baselines_json)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {src: Baseline(source=src, **body) for src, body in data.items()}


def save_baseline(baseline: Baseline, path: Path | None = None) -> Path:
    path = Path(path or SETTINGS.paths.baselines_json)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    body = asdict(baseline)
    body.pop("source")
    data[baseline.source] = body
    # atomic: a partial baselines.json would crash load_baselines (json.loads) on the next run.
    csvio.atomic_write(path, lambda h: h.write(json.dumps(data, indent=2, sort_keys=True)))
    return path
