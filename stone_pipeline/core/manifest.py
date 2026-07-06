"""Run manifest writer (section 2, section 11.3).

A run is reproducible from its manifest: it pins the input hash, the reference
data versions (content hashes), the config hash, the code version, per-stage
counts, and timings. Deterministic: same inputs produce the same manifest body
(timings aside, which are recorded but never feed output).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


def content_hash(path: Path) -> str:
    """Stable content hash of a reference or input file, recorded for repro."""
    path = Path(path)
    if not path.exists():
        return "absent"
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


@dataclass
class StageMetric:
    stage: str
    rows_in: int = 0
    rows_out: int = 0
    rejected: int = 0
    reviewed: int = 0
    gapped: int = 0
    seconds: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Manifest:
    run_id: str
    source: str
    code_version: str
    environment: str
    config_hash: str = ""
    input_path: str = ""
    input_hash: str = ""
    reference_versions: dict[str, str] = field(default_factory=dict)
    health_status: str = ""
    magnitude_status: str = ""  # post-derive canonical magnitude-drift gate -> OK/DEGRADED/FAILED
    gate_status: dict[str, str] = field(default_factory=dict)  # module gate -> OK/DEGRADED/FAILED
    stage_metrics: list[StageMetric] = field(default_factory=list)
    match_method_distribution: dict[str, int] = field(default_factory=dict)
    review_code_counts: dict[str, int] = field(default_factory=dict)
    gap_kind_counts: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    write_backs: list[str] = field(default_factory=list)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def add_stage(self, metric: StageMetric) -> None:
        self.stage_metrics.append(metric)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return path
