"""Source admission (Phase 2): the rule that decides when a data source is 'consistent enough to be allowed
in' (auto-load), and the reaction that pulls a source back out when it drifts.

Reuses what already exists: the per-run health/drift the pipeline records (stages.json + health.json ->
config.db run events), the certify trust gate (certify.certify_source), and the review/auto mode ladder. It
adds only the missing bits: a rolling consistency signal and one config-driven decision.

  - A source is ELIGIBLE for auto once its last `consistent_runs` runs are CLEAN (health OK + no drift) and
    (if required) it passes certification. Promotion itself stays explicit/loud -- eligibility is surfaced,
    the operator (or an opt-in) flips it to auto.
  - A drift event (health not OK, a gate FAILED, or a format-drift item) on a source already on `auto`
    auto-DEMOTES it back to review and records the reason, so bad data can never keep auto-loading.

All thresholds live in settings.admission (no magic numbers here). The decision core (`compute`) is a pure
function of its inputs, so it is unit-tested directly.
"""

from __future__ import annotations

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.admission")

# admission states surfaced to the :4200 UI
REVIEW = "review"      # not yet trusted (the default); output stages for human sign-off
ELIGIBLE = "eligible"  # passed the consistency rule -> may be promoted to auto (still explicit)
AUTO = "auto"          # mode == auto and serving automatically
DEMOTED = "demoted"    # was auto; a drift knocked it back to review (must re-earn eligibility)

_RANK = {"OK": 0, "DEGRADED": 1, "FAILED": 2}


def worst_status(summary: dict) -> str:
    """The worst OK/DEGRADED/FAILED across a run's stages + gates + magnitude (skipped/not_run rank as OK)."""
    statuses = [s.get("status") for s in summary.get("stages", [])]
    statuses += list(summary.get("gates", {}).values())
    statuses.append(summary.get("magnitude") or "OK")
    worst = "OK"
    for st in statuses:
        if _RANK.get(st, 0) > _RANK.get(worst, 0):
            worst = st
    return worst


def _clean(event: dict) -> bool:
    """A run counts toward the consistency streak only when the SOURCE validated cleanly: health OK, no
    format drift, and no FAILED gate. A merely-DEGRADED stage is informational and does not break it."""
    return event.get("health") == "OK" and not event.get("drift") and event.get("worst") != "FAILED"


def streak(events: list[dict]) -> int:
    """Leading consecutive clean runs (events newest-first)."""
    n = 0
    for e in events:
        if not _clean(e):
            break
        n += 1
    return n


def compute(events: list[dict], certified: bool, mode: str, rule=None) -> dict:
    """Pure decision core: the admission state from a source's history + certify result + current mode."""
    rule = rule or SETTINGS.admission
    s = streak(events)
    eligible = s >= rule.consistent_runs and (certified or not rule.require_certify)
    if mode == "auto":
        state = AUTO
    elif events and events[0].get("note") == "demoted":
        state = DEMOTED
    elif eligible:
        state = ELIGIBLE
    else:
        state = REVIEW
    return {"state": state, "streak": s, "required": rule.consistent_runs,
            "certified": certified, "eligible": eligible, "mode": mode,
            "last_drift": events[0].get("drift", []) if events else []}


def _certified(source: str, rule) -> bool:
    """Whether the source passes certification NOW (offline, deterministic). Best-effort: a certify error
    counts as NOT certified (conservative -- never auto-promotes on a broken cert)."""
    if not rule.require_certify:
        return True
    try:
        from stone_pipeline import certify
        return certify.certify_source(source).passed
    except Exception:
        log.exception("certify failed for %s during admission (treated as not certified)", source)
        return False


def record_and_evaluate(source: str, summary: dict, rule=None, path=None) -> dict:
    """Record this run's outcome into the source's history and REACT to a drift. Called by the control plane
    after a produce. If the source is on `auto` and this run drifted (health not OK / a FAILED gate / a
    format-drift item), auto-DEMOTE it to review and record the reason. Returns the action taken."""
    from stone_pipeline.config import store
    from stone_pipeline.config.sources import load_source
    rule = rule or SETTINGS.admission
    health = summary.get("health") or "OK"
    worst = worst_status(summary)
    drift = [d.get("kind") for d in summary.get("drift", []) if d.get("kind")]
    run_id = summary.get("run_id", "")
    mode = getattr(load_source(source), "mode", "review")
    demote = mode == "auto" and (health != "OK" or worst == "FAILED" or bool(drift))
    certified = _certified(source, rule)
    store.record_run_event(source, run_id, health, worst, drift, certified,
                           note="demoted" if demote else None, limit=rule.history_limit, path=path)
    if demote:
        store.set_state(source, mode="review", path=path)
        log.warning("source auto-DEMOTED to review on drift",
                    extra={"extra_fields": {"source": source, "health": health, "worst": worst, "drift": drift}})
    return {"demoted": demote, "health": health, "worst": worst, "drift": drift, "certified": certified}


def status_for(source: str, rule=None, path=None) -> dict:
    """The admission status for the :4200 diagnostics surface: state + consistency streak + certify + last
    drift. Cheap (reads the stored history; no live certify), so it is safe on the read path."""
    from stone_pipeline.config import store
    from stone_pipeline.config.sources import load_source
    rule = rule or SETTINGS.admission
    events = store.read_run_events(source, limit=rule.history_limit, path=path)
    mode = getattr(load_source(source), "mode", "review")
    certified = events[0].get("certified", False) if events else False
    return compute(events, certified, mode, rule)


def evaluate_from_outputs(outputs_dir=None, path=None) -> int:
    """Control-plane post-run pass: for each known source, read its latest run diagnostic from disk and
    record+react (auto-demote on drift). A source whose latest on-disk run is ALREADY the newest recorded
    event is skipped -- it did not run this produce, so re-recording (and re-certifying) it is wasted work.
    Best-effort per source. Returns the number newly processed."""
    from stone_pipeline import adapters as adapter_registry
    from stone_pipeline.config import diagnostics, store
    processed = 0
    for source in adapter_registry.REGISTRY:
        try:
            summary = diagnostics.latest_for_source(source, outputs_dir)
            if not summary:
                continue
            recent = store.read_run_events(source, limit=1, path=path)
            if recent and recent[0]["run_id"] == summary.get("run_id"):
                continue                       # unchanged since last evaluated -> skip the redundant work
            record_and_evaluate(source, summary, path=path)
            processed += 1
        except Exception:
            log.exception("admission evaluation failed for %s (non-fatal)", source)
    return processed
