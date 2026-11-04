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
import sqlite3
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.io import imagestore

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


def pending_apply(path: str | Path | None = None) -> dict | None:
    """The GLOBAL 're-produce to apply' banner for the diagnostics endpoint: operator decisions recorded
    since the last catalog-applying produce are not yet reflected in the emitted catalog. Best-effort and
    isolated (mirrors the images block): returns None on any read error so it never fails the diagnostics
    response. See store.decisions_pending_apply for the (no-scan) computation."""
    from stone_pipeline.config import store
    try:
        return store.decisions_pending_apply(path=path)
    except Exception:
        log.exception("pending-apply signal failed (non-fatal)")
        return None


_STANDARD_MARKER = f"/products/{imagestore.STANDARD_SUBDIR}/"   # a thumbnail under it is a standardised photo


def ledger_image_counts() -> dict[str, dict[str, int]] | None:
    """{source_code: {total, standard, hosted, hidden}} over the listable products of this environment's sync ledger: in
    stock (qty > 0) or hidden for want of a standardised photo (reason no_photo); a sold-out or delisted product is not
    counted. standard: listed with a published standardised photo as its thumbnail; hosted: listed with any thumbnail;
    hidden: reason no_photo. None when there is no ledger (never created here) or it cannot be read: the caller then
    serves no images block, and the failure is logged."""
    from stone_pipeline.config.settings import ENV_NAME
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger, LedgerError, on_sale_sql
    path = writethrough.ledger_path()
    if not path.exists():
        return None
    try:
        with Ledger.open(path, env=ENV_NAME) as ledger:
            rows = ledger.execute(
                "SELECT p.source AS source, COUNT(*) AS total,"
                " SUM(CASE WHEN i.reason IS NOT 'no_photo' AND p.thumbnail_key LIKE ? THEN 1 ELSE 0 END) AS standard,"
                " SUM(CASE WHEN i.reason IS NOT 'no_photo' AND COALESCE(p.thumbnail_key, '') != '' THEN 1 ELSE 0 END)"
                " AS hosted,"
                " SUM(CASE WHEN i.reason = 'no_photo' THEN 1 ELSE 0 END) AS hidden"
                " FROM product p JOIN inventory i ON i.sku = p.sku"
                f" WHERE {on_sale_sql('i')} OR i.reason = 'no_photo' GROUP BY p.source",
                (f"%{_STANDARD_MARKER}%",)).fetchall()
    except (sqlite3.Error, LedgerError):
        log.exception("images block: the ledger could not be read; no images block served")
        return None
    return {r["source"]: {k: r[k] for k in ("total", "standard", "hosted", "hidden")} for r in rows}


def images_block(counts: dict[str, int], image_pipeline: bool, photos_running: bool, held: int) -> dict:
    """A source's `images` block for the admin, over its listable products (ledger_image_counts): total = ready + hidden
    + the rest. ready: listed with a published standardised photo, or, when the source's image pipeline is off, listed
    with a hosted photo (they show the scraped ones). hidden: served at stock 0 with reason no_photo. generating: a
    photos run is under way and works this source."""
    return {"image_pipeline": image_pipeline, "total": counts["total"],
            "ready": counts["standard"] if image_pipeline else counts["hosted"],
            "hidden": counts["hidden"], "generating": photos_running and image_pipeline, "held_for_image": held}


def held_for_image(summary: dict) -> int:
    """Products the last produce could NOT emit but a republish CAN still emit once their image lands -- the
    REPUBLISHABLE subset of the images stage `no_image` count: rows that have a usable source image which is
    not linked yet. It EXCLUDES `no_image_source` (rows with no usable source url at all): those are
    permanent rejects a republish can never fix, so they must not drive a "republish" prompt (they already
    show as rejected in the funnel). So this clears to 0 when nothing is genuinely republishable. Carried in
    the `images` block (images_block). 0 when the
    summary carries no images stage. Back-compat: a summary predating no_image_source subtracts 0 (old
    behaviour)."""
    if not isinstance(summary, dict):
        return 0
    for st in summary.get("stages") or []:
        if isinstance(st, dict) and st.get("stage") == "images":
            extra = st.get("extra") or {}
            try:
                republishable = int(extra.get("no_image") or 0) - int(extra.get("no_image_source") or 0)
            except (TypeError, ValueError):
                return 0
            return max(0, republishable)     # never negative; no_image_source is a subset of no_image
    return 0
