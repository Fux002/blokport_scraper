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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


STAGES = ("scrape", "catalog", "inventory", "all")


def _public(rec: dict) -> dict:
    return {k: rec.get(k) for k in
            ("run_id", "status", "mode", "started_at", "finished_at", "sources", "stage", "progress", "error")}


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

def _watch_local(rec: dict, proc: subprocess.Popen) -> None:
    with _lock:
        rec["status"] = "running"
    rc = proc.wait()
    with _lock:
        rec["status"] = "succeeded" if rc == 0 else "failed"
        rec["finished_at"] = _now()
        if rc != 0:
            rec["error"] = f"pipeline exited {rc}"
    log.info("scraper run finished", extra={"extra_fields": {"run_id": rec["run_id"], "rc": rc}})


def _build_command(rec: dict) -> list[str]:
    """The produce subprocess for this run. FULL produce = stone_pipeline.build (scrape -> catalog ->
    consistency gate), NOT bare `run all`: that refreshes products against a STALE variation table (a
    new variety never enters it), the produce-vs-build divergence. `--stage` scopes HOW FAR (scrape /
    catalog / all) and `--sources` scopes WHICH scrapers (omitted -> every enabled source)."""
    cmd = [sys.executable, "-m", "stone_pipeline.build", "--stage", rec.get("stage", "all")]
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
    command = ["python", "-m", "stone_pipeline.build", "--stage", rec.get("stage", "all")]
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
        rec["status"] = "running"   # runs on the cluster; follow it in CloudWatch


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
    # An explicit request is validated against the registry: a subset with no KNOWN source is a 400
    # (not a silent fall-through to "run everything"); None/empty means every enabled source (scope None).
    scope = _resolve_sources(sources) if sources else None
    if sources and not scope:
        return {"error": f"no known source in {sources!r}"}, 400
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
        _current_id = run_id
    try:
        (launch or _LAUNCHERS[rec["mode"]])(rec)
    except Exception as exc:                       # a failed launch is a completed (failed) run
        with _lock:
            rec["status"] = "failed"
            rec["finished_at"] = _now()
            rec["error"] = str(exc)
        log.exception("scraper run failed to launch")
        return _public(rec), 500
    return _public(rec), 202


def get_run(run_id: str) -> dict | None:
    with _lock:
        rec = _runs.get(run_id)
        return _public(rec) if rec else None


def current() -> dict:
    with _lock:
        rec = _runs.get(_current_id) if _current_id else None
        return {"current": _public(rec) if rec else None}
