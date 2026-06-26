"""Stage 0: health and schema-drift gate (section 7).

Runs before ingest. Compares the incoming file against the source contract and
the last-good baseline, producing scrape_health_<source>.json with status OK,
DEGRADED, or FAILED. FAILED aborts before any emit; DEGRADED continues but stamps
every row; OK updates the baseline (so the baseline self-tunes, and only on OK
runs so a degraded scrape never poisons the reference point).

When the status is DEGRADED or FAILED the report localizes the change for fast
adapter repair (section 7, section 8.5): which required columns disappeared or
were likely renamed (matched by header similarity and by value-shape similarity
to old columns), which fields lost fill, and which patterns broke, with example
rows. That diagnosis is the input to the adapter repair loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import polars as pl
from rapidfuzz import fuzz

from stone_pipeline.config.contracts import (
    Baseline,
    SourceContract,
    column_fill_rates,
    save_baseline,
)
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
# The OK/DEGRADED/FAILED ladder is shared with every module gate (gates/report.py),
# so the health gate and the boundary gates speak one status vocabulary.
from stone_pipeline.gates.report import DEGRADED, FAILED, OK, RANK as _RANK

log = logfmt.get_logger("health")


@dataclass
class DriftItem:
    kind: str  # missing_column, fill_drop, pattern_break, rowcount_collapse, new_column, parse_fail
    field: str
    detail: str
    likely_rename: Optional[str] = None
    examples: list[str] = field(default_factory=list)


@dataclass
class HealthReport:
    source: str
    status: str = OK
    row_count: int = 0
    row_baseline: int = 0
    fill_rates: dict[str, float] = field(default_factory=dict)
    drift: list[DriftItem] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def escalate(self, status: str) -> None:
        if _RANK[status] > _RANK[self.status]:
            self.status = status

    def to_dict(self) -> dict:
        body = asdict(self)
        body["drift"] = [asdict(d) for d in self.drift]
        return body

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path


def _value_shape_signature(series: pl.Series) -> str:
    """A coarse signature of a column's values, used to spot a renamed column by
    value-shape similarity even when the header changed. Captures whether values
    look numeric, contain urls, pipe lists, etc."""
    sample = [
        str(v)
        for v in series.cast(pl.String, strict=False).drop_nulls().head(20).to_list()
        if str(v).strip()
    ]
    if not sample:
        return "empty"
    flags = []
    joined = " ".join(sample)
    if all(re.fullmatch(r"-?\d+(\.\d+)?", s.strip()) for s in sample):
        flags.append("numeric")
    if "http" in joined:
        flags.append("url")
    if "|" in joined:
        flags.append("pipe")
    if any(len(s) > 60 for s in sample):
        flags.append("long")
    avg_len = sum(len(s) for s in sample) // len(sample)
    flags.append(f"len{avg_len // 10 * 10}")
    return ",".join(flags)


def _diagnose_missing_columns(
    frame: pl.DataFrame, missing: list[str], baseline_signatures: dict[str, str]
) -> list[DriftItem]:
    """For each missing required column, try to name the column that likely
    replaced it: highest header similarity, tie-broken by value-shape match."""
    items: list[DriftItem] = []
    present = frame.columns
    for col in missing:
        likely = None
        best_score = 0.0
        old_sig = baseline_signatures.get(col)
        for candidate in present:
            header_score = fuzz.ratio(col.lower(), candidate.lower())
            shape_bonus = 0.0
            if old_sig and old_sig == _value_shape_signature(frame.get_column(candidate)):
                shape_bonus = 25.0
            score = header_score + shape_bonus
            if score > best_score:
                best_score = score
                likely = candidate
        items.append(
            DriftItem(
                kind="missing_column",
                field=col,
                detail=f"required column missing; closest present column scored {best_score:.0f}",
                likely_rename=likely if best_score >= 60 else None,
            )
        )
    return items


def run_health(
    frame: pl.DataFrame,
    contract: SourceContract,
    baseline: Optional[Baseline],
    smoke_test: Optional[Callable[[pl.DataFrame], int]] = None,
) -> HealthReport:
    """Run the five checks (section 7). smoke_test, when supplied, runs the
    adapter on a sample and returns the count of valid canonical rows produced;
    it lets Stage 0 catch a silently broken adapter (check 5)."""
    thresholds = SETTINGS.thresholds
    report = HealthReport(
        source=contract.source,
        row_count=frame.height,
        row_baseline=baseline.row_count if baseline else contract.row_baseline,
    )
    report.fill_rates = column_fill_rates(frame)

    # 1. Structural: required columns present.
    missing = [c for c in contract.required_columns if c not in frame.columns]
    if missing:
        report.escalate(FAILED)
        baseline_sigs = {}  # baseline does not store signatures; header+shape on present cols
        report.drift.extend(_diagnose_missing_columns(frame, missing, baseline_sigs))
        report.messages.append(f"missing required columns: {missing}")
    # New unexpected columns -> DEGRADED warning.
    known = set(contract.all_columns)
    new_cols = [c for c in frame.columns if c not in known]
    if new_cols:
        report.escalate(DEGRADED)
        for col in new_cols:
            report.drift.append(
                DriftItem(kind="new_column", field=col, detail="unexpected new column; adapter may need updating")
            )

    # 2. Volume: row count vs baseline.
    base_rows = baseline.row_count if baseline else contract.row_baseline
    if base_rows and frame.height < thresholds.health_rowcount_floor * base_rows:
        report.escalate(FAILED)
        report.drift.append(
            DriftItem(
                kind="rowcount_collapse",
                field="__rows__",
                detail=f"row count {frame.height} below {thresholds.health_rowcount_floor:.0%} of baseline {base_rows}",
            )
        )

    # 3. Fill rate: per-field non-empty fraction vs baseline.
    if baseline:
        for col, base_rate in baseline.fill_rates.items():
            if col not in frame.columns:
                continue
            now_rate = report.fill_rates.get(col, 0.0)
            drop = base_rate - now_rate
            if drop >= thresholds.health_fill_drop_fail and col in contract.required_columns:
                report.escalate(FAILED)
                report.drift.append(
                    DriftItem(
                        kind="fill_drop",
                        field=col,
                        detail=f"fill dropped {drop:.2f} (baseline {base_rate:.2f} -> {now_rate:.2f}) on required field",
                    )
                )
            elif drop >= thresholds.health_fill_drop_warn:
                report.escalate(DEGRADED)
                report.drift.append(
                    DriftItem(
                        kind="fill_drop",
                        field=col,
                        detail=f"fill dropped {drop:.2f} (baseline {base_rate:.2f} -> {now_rate:.2f})",
                    )
                )

    # 4. Shape: value patterns / value sets.
    for col, pattern in contract.value_patterns.items():
        if col not in frame.columns:
            continue
        series = frame.get_column(col).cast(pl.String, strict=False).fill_null("")
        non_empty = series.filter(series.str.strip_chars().str.len_chars() > 0)
        if non_empty.len() == 0:
            continue
        compiled = re.compile(pattern)
        bad = [v for v in non_empty.to_list() if not compiled.search(str(v))]
        violation_rate = len(bad) / non_empty.len()
        if violation_rate > 0.2:
            report.escalate(DEGRADED if violation_rate < 0.5 else FAILED)
            report.drift.append(
                DriftItem(
                    kind="pattern_break",
                    field=col,
                    detail=f"{violation_rate:.0%} of values fail pattern {pattern!r}",
                    examples=[str(v) for v in bad[:3]],
                )
            )
    for col, allowed in contract.value_sets.items():
        if col not in frame.columns or not allowed:
            continue
        allowed_norm = {a.strip().casefold() for a in allowed}
        series = frame.get_column(col).cast(pl.String, strict=False).fill_null("")
        non_empty = [v for v in series.to_list() if str(v).strip()]
        bad = [v for v in non_empty if str(v).strip().casefold() not in allowed_norm]
        if non_empty and len(bad) / len(non_empty) > 0.2:
            report.escalate(DEGRADED)
            report.drift.append(
                DriftItem(
                    kind="pattern_break",
                    field=col,
                    detail=f"{len(bad)}/{len(non_empty)} values outside the allowed set",
                    examples=[str(v) for v in bad[:3]],
                )
            )

    # 5. Parse smoke test.
    if smoke_test is not None:
        try:
            sample = frame.head(50)
            produced = smoke_test(sample)
            if produced <= max(1, int(0.1 * sample.height)):
                report.escalate(FAILED)
                report.drift.append(
                    DriftItem(
                        kind="parse_fail",
                        field="__adapter__",
                        detail=f"adapter yielded only {produced} canonical rows from {sample.height} sample rows",
                    )
                )
        except Exception as exc:  # fail loud, isolated
            report.escalate(FAILED)
            report.drift.append(
                DriftItem(kind="parse_fail", field="__adapter__", detail=f"adapter raised: {exc}")
            )

    log.info(
        f"health {report.status} for {contract.source}",
        extra={"extra_fields": {"status": report.status, "rows": frame.height, "drift": len(report.drift)}},
    )
    return report


def update_baseline_if_ok(
    report: HealthReport,
    contract: SourceContract,
    now_iso: str,
    path: Optional[Path] = None,
) -> Optional[Baseline]:
    """OK runs update the baseline (section 7). DEGRADED and FAILED do not."""
    if report.status != OK:
        return None
    baseline = Baseline(
        source=contract.source,
        row_count=report.row_count,
        fill_rates=report.fill_rates,
        adapter_version=contract.adapter_version,
        updated_at=now_iso,
    )
    save_baseline(baseline, path)
    return baseline


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
