"""Per-source pipeline diagnostics for the admin API (Phase 1 layer diagnostics).

The pipeline writes, per run, a per-layer `stages.json` (each stage's OK/DEGRADED/FAILED status + counts)
and a `health.json` (the Validate-in drift detector: missing/new column, fill_drop, likely_rename). This
module reads the LATEST run per source, folds the health DRIFT into one compact summary, and:
  - persists it into config.db after a produce (the control plane), so GET /config/v1/diagnostics is
    durable across a config-server restart and needs no per-request disk scan; and
  - serves it (config.db first, on-disk latest as a fallback) so a source produced from the CLI -- with no
    control-plane persist -- is still visible when the server shares the run disk.

So a silent source format change surfaces per source in the :4200 UI: a red stage + the exact drift item.
"""

from __future__ import annotations

import json
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.diagnostics")


def _outputs_dir(outputs_dir: str | Path | None) -> Path:
    return Path(outputs_dir or SETTINGS.paths.outputs_dir)


def _latest_run_dir(source: str, outputs_dir: Path) -> Path | None:
    """The newest `outputs/<source>_<ts>/` that carries a stages.json. run_id is `<source>_<token>` and a
    source name has no underscore, so the glob is unambiguous; newest sorts last by the timestamp token."""
    candidates = sorted(outputs_dir.glob(f"{source}_*"), reverse=True)
    for run_dir in candidates:
        if (run_dir / "diagnostics" / "stages.json").exists():
            return run_dir
    return None


def _summary_from_disk(run_dir: Path) -> dict:
    """The compact summary served/persisted: the stages.json doc (per-stage status + health/gate/magnitude)
    plus the health DRIFT items, so the UI shows both WHICH layer degraded and WHAT changed in the source."""
    diag = run_dir / "diagnostics"
    doc = json.loads((diag / "stages.json").read_text(encoding="utf-8"))
    health = diag / "health.json"
    if health.exists():
        h = json.loads(health.read_text(encoding="utf-8"))
        doc["drift"] = h.get("drift", [])
        doc["row_count"] = h.get("row_count")
        doc["row_baseline"] = h.get("row_baseline")
    return doc


def latest_for_source(source: str, outputs_dir: str | Path | None = None) -> dict | None:
    """The latest on-disk diagnostic summary for one source, or None if it has never produced one here."""
    run_dir = _latest_run_dir(source, _outputs_dir(outputs_dir))
    return _summary_from_disk(run_dir) if run_dir else None


def persist_from_outputs(outputs_dir: str | Path | None = None,
                         path: str | Path | None = None) -> int:
    """Read each known source's latest run diagnostic from disk and store it in config.db. Called by the
    control plane after a produce (the config-server process, which shares the run disk in dev). Best-effort
    per source: one unreadable run never blocks the others. Returns the number of sources persisted."""
    outputs_dir = _outputs_dir(outputs_dir)
    from stone_pipeline import adapters as adapter_registry
    from stone_pipeline.config import store
    persisted = 0
    for source in adapter_registry.REGISTRY:
        try:
            summary = latest_for_source(source, outputs_dir)
            if summary:
                store.record_source_diagnostic(source, summary.get("run_id", ""), summary, path=path)
                persisted += 1
        except Exception:
            log.exception("persist diagnostic failed for %s (non-fatal)", source)
    return persisted


def read_source(source: str, path: str | Path | None = None,
                outputs_dir: str | Path | None = None) -> dict | None:
    """One source's diagnostic for the API: the durable config.db row, falling back to the latest on-disk
    run when the store has none yet (a CLI produce that never went through the control-plane persist),
    enriched with the source's admission state (review | eligible | auto | demoted) + consistency streak."""
    from stone_pipeline.config import admission, store
    summary = store.get_source_diagnostic(source, path=path) or latest_for_source(source, outputs_dir)
    if summary is None:
        return None
    try:
        summary["admission"] = admission.status_for(source, path=path)
    except Exception:
        log.exception("admission status failed for %s (non-fatal)", source)
    return summary


def read_all(path: str | Path | None = None) -> list[dict]:
    """Every source's latest persisted diagnostic (durable store only; the per-source route does the
    disk fallback for a source not yet persisted)."""
    from stone_pipeline.config import store
    return store.read_source_diagnostics(path=path)
