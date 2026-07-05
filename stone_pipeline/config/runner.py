"""On-demand scraper-run trigger for the admin (:4200) -- the missing "produce" step.

`start_run()` kicks off `run all` for the ENABLED sources (run_all already filters by the config
store's enabled flags), asynchronously, filling the sync ledger. Then Medusa's catalog/inventory
import pulls that ledger into the shop. Two backends, chosen by BLOKPORT_RUN_MODE:

  local (dev default)  a subprocess running the pipeline on this host; its exit code is tracked.
  ecs   (prod)         triggers the SAME scheduled Fargate task on demand (aws ecs run-task); the
                       task runs on the cluster and is watched in CloudWatch, not here.

The nightly schedule is untouched -- this is the manual "scrape now" path alongside it. Single-run
guarded: a second trigger while one is in progress is refused (409). Run state is in-memory (one
control-plane process); it resets if the server restarts, which is fine for a manual trigger.

Contract (matches the :4200 admin's expectations):
  run record = { run_id, status, mode, started_at, finished_at, sources, progress, error }
  status     = queued -> running -> succeeded | failed
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone

from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.runner")

_lock = threading.Lock()
_runs: dict[str, dict] = {}      # run_id -> record (kept so GET /run/{id} works after it finishes)
_current_id: str | None = None   # the in-flight run, if any
_MAX_RUNS = 200                  # cap the in-memory history so a long-lived server never grows unbounded (M2)


def run_in_progress() -> bool:
    """True while a produce run is queued/running (status-based). stone_pipeline.lifecycle checks this so a
    destructive op refuses to run mid-produce; the run <-> op exclusion lives in lifecycle (single lock)."""
    with _lock:
        return _current_id is not None and _runs.get(_current_id, {}).get("status") in ("queued", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


STAGES = ("scrape", "catalog", "inventory", "all")


def _public(rec: dict) -> dict:
    return {k: rec.get(k) for k in
            ("run_id", "status", "mode", "started_at", "finished_at",
             "sources", "scope", "stage", "counts", "progress", "error")}


def _capture_counts() -> dict | None:
    """Ledger totals per entity after a run, for the run record's `counts` (what's now in the ledger).
    Best-effort: a failure here must not affect the run."""
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    try:
        with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
            return {t: lg.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                    for t in ("variation", "product", "inventory")}
    except Exception:
        log.exception("failed to capture run counts (non-fatal)")
        return None


def _persist_run(rec: dict) -> None:
    """Durably store a terminal run record so GET /run can return `last` across a restart."""
    try:
        from stone_pipeline.config import store
        store.record_run_log(_public(rec))
    except Exception:
        log.exception("failed to persist run record (non-fatal)")


def _stamp_last_run(rec: dict, status: str) -> None:
    """Persist this run against each of its sources (the admin list's 'last run' label). Best-effort:
    a store failure must never affect the run. `scope` (the explicit subset) is what actually ran when
    set; otherwise every enabled source in `sources` did (stage all/catalog/inventory over the lot)."""
    try:
        from stone_pipeline.config import store
        store.record_run(rec.get("scope") or rec.get("sources") or [], status, rec.get("stage", "all"))
    except Exception:
        log.exception("last-run stamp failed (non-fatal)")


def _mode() -> str:
    return "ecs" if os.environ.get("BLOKPORT_RUN_MODE", "").strip().lower() == "ecs" else "local"


def _resolve_sources(requested) -> list[str]:
    """The sources this run will scrape: an explicit request intersected with the registry, else the
    ENABLED set (None config store -> whatever the pipeline finds)."""
    from stone_pipeline import adapters as adapter_registry   # adapter_registry.REGISTRY (auto-discovered)
    known = set(adapter_registry.REGISTRY)
    if requested:
        return sorted(s for s in requested if s in known)
    from stone_pipeline.config import store
    names = store.enabled_names()
    return sorted(names) if names is not None else sorted(known)


# -- launchers (injectable for tests) -----------------------------------------

_RUN_TIMEOUT = int(os.environ.get("BLOKPORT_RUN_TIMEOUT_SECONDS", "7200"))   # 2h: kill a wedged produce so
                                                                            # a hung scraper never wedges the
                                                                            # control plane (409s all runs/ops)


def _watch_local(rec: dict, proc: subprocess.Popen) -> None:
    with _lock:
        rec["status"] = "running"
    try:
        rc = proc.wait(timeout=_RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()                                     # a hung produce must not hold the run slot forever
        proc.wait()
        rc = -1
        log.error("produce run exceeded timeout; killed", extra={"extra_fields": {
            "run_id": rec["run_id"], "timeout_s": _RUN_TIMEOUT}})
    counts = _capture_counts()                          # ledger totals after the run
    with _lock:
        rec["status"] = "succeeded" if rc == 0 else "failed"
        rec["finished_at"] = _now()
        rec["counts"] = counts
        if rc != 0:
            rec["error"] = f"pipeline exited {rc}"
    _stamp_last_run(rec, rec["status"])
    _persist_run(rec)                                   # durable `last` across a restart
    log.info("scraper run finished", extra={"extra_fields": {"run_id": rec["run_id"], "rc": rc}})


def _build_command(rec: dict) -> list[str]:
    """The produce subprocess for this run. FULL produce = stone_pipeline.produce (fetch export inputs
    -> LIVE scrape -> build), NOT bare `stone_pipeline.build`: build assumes data/ is already scraped
    (the laptop path), so on a fresh host (a new ECS task with empty data/) it would produce nothing.
    produce guarantees the inputs first. `--stage` scopes HOW FAR (scrape / catalog / all) and
    `--sources` scopes WHICH scrapers (omitted -> every enabled source)."""
    cmd = [sys.executable, "-m", "stone_pipeline.produce", "--stage", rec.get("stage", "all")]
    if rec.get("scope"):
        cmd += ["--sources", ",".join(rec["scope"])]
    return cmd


def _launch_local(rec: dict) -> None:
    proc = subprocess.Popen(
        _build_command(rec),
        env={**os.environ, "BLOKPORT_LEDGER_WRITETHROUGH": "1"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Thread(target=_watch_local, args=(rec, proc), daemon=True).start()


def _launch_ecs(rec: dict) -> None:
    import boto3
    ecs = boto3.client("ecs", region_name=os.environ.get("BLOKPORT_S3_REGION", "eu-west-1"))
    env = os.environ.get("BLOKPORT_ENV", "development")
    # scope the task the same way the local launcher does: override the container's command with the
    # run's stage + sources. The container name must match the task definition's (BLOKPORT_ECS_CONTAINER,
    # default blokport-scraper-<env>). Without stage/scope the taskdef's default command (full build) runs.
    container = os.environ.get("BLOKPORT_ECS_CONTAINER", f"blokport-scraper-{env}")
    command = ["python", "-m", "stone_pipeline.produce", "--stage", rec.get("stage", "all")]
    if rec.get("scope"):
        command += ["--sources", ",".join(rec["scope"])]
    resp = ecs.run_task(
        cluster=os.environ["BLOKPORT_ECS_CLUSTER"],
        taskDefinition=os.environ.get("BLOKPORT_ECS_TASKDEF", f"blokport-scraper-{env}"),
        launchType="FARGATE", count=1,
        overrides={"containerOverrides": [{"name": container, "command": command}]},
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": os.environ["BLOKPORT_ECS_SUBNETS"].split(","),
            "securityGroups": os.environ["BLOKPORT_ECS_SG"].split(","),
            "assignPublicIp": "DISABLED"}})
    with _lock:
        rec["task_arn"] = (resp.get("tasks") or [{}])[0].get("taskArn")
        # ECS is fire-and-forget: nobody in THIS process watches the Fargate task, so it must NOT
        # occupy the single-run slot forever (that would 409 every future run + reset). Mark it
        # 'dispatched' (a terminal-for-us state the run/reset gate ignores) and free the slot; follow
        # the actual task in CloudWatch. 'one run at a time' for ECS is enforced by the cluster.
        rec["status"] = "dispatched"
        rec["finished_at"] = _now()   # our terminal event (we can't watch Fargate); counts stay None
        global _current_id
        _current_id = None
    _persist_run(rec)                 # so it shows as `last` (dispatched, no counts)


_LAUNCHERS = {"local": _launch_local, "ecs": _launch_ecs}


# -- public API ---------------------------------------------------------------

def start_run(sources=None, stage="all", launch=None) -> tuple[dict, int]:
    """Kick off a produce. `sources` None -> every enabled source, else an explicit subset; `stage`
    is scrape / catalog / all (how far to run). Returns (record, http_status): 202 started, 409 if a
    run is already in flight, 400 on a bad stage. `scope` (the explicit subset, internal) is what
    actually scopes the subprocess; `sources` is the resolved list shown to the caller."""
    stage = (stage or "all").strip().lower()
    if stage not in STAGES:
        return {"error": f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}"}, 400
    scope = None
    if sources:
        known = _resolve_sources(sources)
        unknown = [s for s in sources if s not in set(known)]
        if unknown:                            # reject ANY unknown, never silently drop it
            return {"error": f"unknown source(s): {unknown}"}, 400
        # catalog is a SHARED, all-source consolidation: a source scope is meaningless there (build
        # ignores --sources for catalog), so drop it -- else the 'last run' label would credit one source.
        scope = None if stage == "catalog" else known
    from stone_pipeline import lifecycle
    global _current_id
    with _lock:
        if _current_id and _runs[_current_id]["status"] in ("queued", "running"):
            return _public(_runs[_current_id]), 409
        run_id = _now().translate({ord(c): None for c in ":-.T"})[:17]
        rec = {"run_id": run_id, "status": "queued", "mode": _mode(),
               "started_at": _now(), "finished_at": None, "error": None,
               "sources": scope if scope is not None else _resolve_sources(None),
               "stage": stage, "scope": scope, "progress": {}}
        _runs[run_id] = rec
        _current_id = run_id                       # claim the run slot FIRST -> run_in_progress() is now True
        # M2: keep only the newest _MAX_RUNS in memory (durable history is in the run_log). Evict the
        # oldest first (dicts preserve insertion order); never evict the in-flight run.
        while len(_runs) > _MAX_RUNS:
            oldest = next(iter(_runs))
            if oldest == _current_id:
                break
            del _runs[oldest]
    # Check the peer AFTER claiming (mirrors the op's claim-then-check). Claiming first closes the TOCTOU
    # window: an op that starts now will see run_in_progress()==True and back off; if instead an op already
    # holds the slot, we roll our claim back and refuse. One of the two always sees the other -- they can
    # never both proceed (worst case both back off and retry).
    if (busy := lifecycle.active()):
        with _lock:
            rec["status"] = "failed"
            rec["finished_at"] = _now()
            rec["error"] = f"a {busy} is in progress; refusing to run"
            _current_id = None
        return {"error": f"a {busy} is in progress; refusing to run"}, 409
    _stamp_last_run(rec, "running")                # list shows this source as running immediately
    try:
        (launch or _LAUNCHERS[rec["mode"]])(rec)
    except Exception as exc:                       # a failed launch is a completed (failed) run
        with _lock:
            rec["status"] = "failed"
            rec["finished_at"] = _now()
            rec["error"] = str(exc)
        _stamp_last_run(rec, "failed")
        _persist_run(rec)
        log.exception("scraper run failed to launch")
        return _public(rec), 500
    return _public(rec), 202


def _source_codes(names) -> list[str]:
    """Resolve source NAMES (polonine) to the product SKU prefixes / source_codes (pol) the ledger
    stores, dropping any unknown name. Empty result for an all-unknown request (caller returns 400)."""
    from stone_pipeline.config.sources import load_source
    codes = []
    for n in names or []:
        try:
            codes.append(load_source(n).source_code)
        except Exception:
            pass
    return codes



def get_run(run_id: str) -> dict | None:
    with _lock:
        rec = _runs.get(run_id)
        return _public(rec) if rec else None


def current() -> dict:
    """GET /config/v1/run: the in-flight run (or null when idle) AND the last finished run. `last` is
    read from the durable run_log, so it survives a config-server restart (the in-memory dict does not)."""
    with _lock:
        rec = _runs.get(_current_id) if _current_id else None
        cur = _public(rec) if rec and rec["status"] in ("queued", "running") else None
    try:
        from stone_pipeline.config import store
        last = store.last_run_log()
    except Exception:
        log.exception("failed to read last run (non-fatal)")
        last = None
    return {"current": cur, "last": last}
