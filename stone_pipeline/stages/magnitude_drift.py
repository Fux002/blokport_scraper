"""Canonical magnitude-drift gate (post-derive).

Runs after derivation, on the CANONICAL rows, and aborts before emit if the physical quantities have
rescaled. It is the magnitude counterpart to the Stage 0 health gate (which guards raw structure), one
layer down.

Source-AGNOSTIC by construction, which is what makes it scale to many feeds: it checks only the canonical
physical-magnitude fields (weight, length, width, height) that EVERY adapter normalizes to. Because the
field set is the canonical schema -- fixed, not per-source -- a new feed inherits drift protection with
ZERO per-adapter config. And because volatile business values (prices, ages, ids, counters) are NOT
canonical magnitudes, they are never in the checked set, so a legitimate price/stock swing can never
false-fail a healthy scrape.

A unit or normalization error (a per-bundle vs per-piece weight, a metres<->cm swap, an adapter bug)
shifts a magnitude's MEDIAN by a large factor vs the source's last-good baseline; normal run-to-run
variation and physical spread do not. WARN (>= 4x) continues and stamps the manifest; FAIL (>= 10x)
aborts before emit. Self-tuning: the baseline is per source and updates on OK runs only, so a degraded
run never poisons the reference (mirrors the health baseline).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.gates.report import DEGRADED, FAILED, OK, worse

log = logfmt.get_logger("magnitude_drift")

# The canonical physical quantities every adapter normalizes to (core/schema.py CanonicalRow). Fixed and
# source-agnostic: adding a feed adds nothing here. Prices/ages/ids are deliberately absent.
MAGNITUDE_FIELDS = ("weight", "length", "width", "height")
_WARN_FACTOR = 4.0
_FAIL_FACTOR = 10.0
# Medians are bucketed by product FORMAT (slab/block/tile): weight/dims span orders of magnitude across
# formats (a tile ~0.01t, a slab ~0.3t, a block ~20t), so an aggregate median would move with the product
# MIX (a source like marenostone mixes all three). Per-format, a bucket's median is mix-invariant -- only
# a genuine unit/normalization drift moves it. A format with too few rows for a stable median is skipped.
_MIN_SAMPLE = 8


@dataclass
class MagnitudeBaseline:
    source: str
    medians: dict[str, dict[str, float]] = field(default_factory=dict)   # format -> field -> median
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path() -> Path:
    return Path(SETTINGS.paths.magnitude_baselines_json)


def load_baseline(source: str, path: Path | None = None) -> Optional[MagnitudeBaseline]:
    path = Path(path or _path())
    if not path.exists():
        return None
    body = json.loads(path.read_text(encoding="utf-8")).get(source)
    if not body:
        return None
    # explicit field pick (not **body) so a future/extra key in the json can never crash the load
    return MagnitudeBaseline(source=source, medians=body.get("medians", {}),
                             updated_at=body.get("updated_at", ""))


def save_baseline(baseline: MagnitudeBaseline, path: Path | None = None) -> None:
    path = Path(path or _path())
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    body = asdict(baseline)
    body.pop("source")
    data[baseline.source] = body
    # atomic: a partial json would crash the next load
    csvio.atomic_write(path, lambda h: h.write(json.dumps(data, indent=2, sort_keys=True)))


def _fmt(row: CanonicalRow) -> str:
    return (getattr(row, "format_value", None) or "unknown").strip().lower()


def medians(rows: list[CanonicalRow]) -> dict[str, dict[str, float]]:
    """{format: {field: median}} for each canonical magnitude field, bucketed by product format. Only
    positive values count; a format with fewer than _MIN_SAMPLE rows is skipped (too small a sample for a
    stable median), and a field/format with no positive value is simply absent."""
    from collections import defaultdict
    by_format: dict[str, list[CanonicalRow]] = defaultdict(list)
    for r in rows:
        by_format[_fmt(r)].append(r)
    out: dict[str, dict[str, float]] = {}
    for fmt, frows in by_format.items():
        if len(frows) < _MIN_SAMPLE:
            continue
        fields: dict[str, float] = {}
        for f in MAGNITUDE_FIELDS:
            vals = [v for r in frows if (v := getattr(r, f, None)) is not None and v > 0]
            if vals:
                fields[f] = round(statistics.median(vals), 4)
        if fields:
            out[fmt] = fields
    return out


def check(rows: list[CanonicalRow], source: str, path: Path | None = None) -> tuple[str, dict]:
    """Compare this run's canonical magnitude medians to the source's last-good baseline. Returns
    (status, report). On OK the baseline is updated (self-tuning); a FAILED status means the caller
    should abort before emit. The first run for a source (no baseline) is OK and seeds the baseline."""
    current = medians(rows)
    baseline = load_baseline(source, path)
    status = OK
    drift: list[dict] = []
    if baseline:
        for fmt, base_fields in baseline.medians.items():
            cur_fields = current.get(fmt, {})
            for f, base_med in base_fields.items():
                now_med = cur_fields.get(f)
                if now_med is None or base_med <= 0:   # format/field absent this run -> nothing to compare
                    continue
                factor = max(now_med / base_med, base_med / now_med)
                if factor >= _FAIL_FACTOR:
                    status = worse(status, FAILED)
                elif factor >= _WARN_FACTOR:
                    status = worse(status, DEGRADED)
                else:
                    continue
                drift.append({"format": fmt, "field": f, "factor": round(factor, 1),
                              "baseline": base_med, "now": now_med})
    report = {"source": source, "status": status, "medians": current, "drift": drift}
    if status == OK:
        # MERGE, do not replace: a format transiently below _MIN_SAMPLE (a short scrape, a category briefly
        # out of stock) is absent from `current`; carrying its last-good median forward keeps the gate
        # armed for it next run. Formats present this run refresh; absent ones are preserved.
        merged = dict(baseline.medians) if baseline else {}
        merged.update(current)
        save_baseline(MagnitudeBaseline(source=source, medians=merged, updated_at=_now()), path)
    log.info(f"magnitude drift {status} for {source}",
             extra={"extra_fields": {"status": status, "drift": len(drift), "fields": list(current)}})
    return status, report
