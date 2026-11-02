"""On-demand scraper-run trigger for the admin (:4200) -- the missing "produce" step.

`start_run()` kicks off `run all` for the ENABLED sources (run_all already filters by the config
store's enabled flags), asynchronously, filling the sync ledger. Then Medusa's catalog/inventory
import pulls that ledger into the shop. ONE launcher: a produce subprocess on this host (the config
container), streamed and watched here, with its exit code tracked. There is no second orchestrator (no
cron task, no fire-and-forget Fargate dispatch): an unattended schedule is an EventBridge target on this
API, so every produce goes through the same gates and the same ledger. Single-run
guarded: a second trigger while one is in progress is refused (409), except that a photos run (the
standardised-photo process) gives way: any other stage asked for while it runs stops it cleanly and runs at
once, so the owner's Publish is never blocked; the photos run's work is kept for the next photos run. Run
state is in-memory (one control-plane process); it resets if the server restarts, which is fine for a manual
trigger.

Contract (matches the :4200 admin's expectations):
  run record = { run_id, status, mode, started_at, finished_at, sources, progress, error }
  status     = queued -> running -> succeeded | failed | stopped (a photos run that gave way)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from stone_pipeline.config.settings import RUN_STOP_GRACE_SECONDS, RUN_TIMEOUT_SECONDS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.runner")

_lock = threading.Lock()
_runs: dict[str, dict] = {}      # run_id -> record (kept so GET /run/{id} works after it finishes)
_current_id: str | None = None   # the in-flight run, if any
# The running run's subprocess, the run it belongs to and the thread watching it (it finishes the run record). Set
# under _lock in the same section that checks whether the run may still start (_launch_local).
_child: subprocess.Popen | None = None
_child_run: dict | None = None
_watcher: threading.Thread | None = None
_stopping = False                        # the server is stopping: no run starts any more
_MAX_RUNS = 200                  # cap the in-memory history so a long-lived server never grows unbounded (M2)


def run_in_progress() -> bool:
    """True while a produce run is queued/running (status-based). stone_pipeline.lifecycle checks this so a
    destructive op refuses to run mid-produce; the run <-> op exclusion lives in lifecycle (single lock)."""
    with _lock:
        return _current_id is not None and _runs.get(_current_id, {}).get("status") in ("queued", "running")


def photos_in_progress() -> bool:
    """True while a photos run (the standardised-photo process) is queued or running: the admin's images block reads
    it as `generating`."""
    with _lock:
        rec = _runs.get(_current_id) if _current_id else None
        return rec is not None and rec.get("stage") == "photos" and rec.get("status") in ("queued", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Kept in sync with build.STAGES (a test locks the two together). Duplicated here on purpose: the config
# server validates the :4200 trigger WITHOUT importing the heavy pipeline (build pulls catalog/run/inventory).
# `republish` = re-run pipeline + catalog from the last scrape, no supplier re-fetch (release approved products).
# `photos` is this runner's own stage, not a produce: the standardised-photo process (deploy.standardise_run,
# STANDARDISE_PROCESS.md) over the whole environment, which links what it published with a republish of its own.
STAGES = ("scrape", "catalog", "republish", "inventory", "all", "photos")
# With STANDARDISE.AUTO on, the photos run follows a full produce (the owner's button) and nothing else. A republish
# (Blokport's Publish button) or any other stage asked for while a photos run holds the slot stops it and runs at once
# (start_run), so the photos run never blocks the owner.
_PHOTOS_AFTER = ("all",)


def _public(rec: dict) -> dict:
    return {k: rec.get(k) for k in
            ("run_id", "status", "started_at", "finished_at",
             "sources", "scope", "stage", "close_only", "counts", "progress", "error")}


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


def _persist_diagnostics() -> None:
    """Fold each source's latest per-layer diagnostic (written to outputs/ by the pipeline) into config.db,
    so GET /config/v1/diagnostics serves the :4200 UI durably. This process shares the run disk in dev.
    Best-effort: a diagnostics failure must never affect the run."""
    try:
        from stone_pipeline.config import diagnostics
        diagnostics.persist_from_outputs()
    except Exception:
        log.exception("failed to persist per-source diagnostics (non-fatal)")


def _evaluate_admission() -> None:
    """Record each source's run outcome into its consistency history and REACT: a drift on an `auto` source
    auto-demotes it to review. Best-effort: an admission failure must never affect the run."""
    try:
        from stone_pipeline.config import admission
        admission.evaluate_from_outputs()
    except Exception:
        log.exception("failed to evaluate source admission (non-fatal)")


def _stamp_last_run(rec: dict, status: str) -> None:
    """Persist this run against each of its sources (the admin list's 'last run' label). Best-effort:
    a store failure must never affect the run. `scope` (the explicit subset) is what actually ran when
    set; otherwise every enabled source in `sources` did (stage all/catalog/inventory over the lot)."""
    try:
        from stone_pipeline.config import store
        store.record_run(rec.get("scope") or rec.get("sources") or [], status, rec.get("stage", "all"))
    except Exception:
        log.exception("last-run stamp failed (non-fatal)")


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

_RUN_TIMEOUT = RUN_TIMEOUT_SECONDS     # kill a wedged produce so a hung scraper never wedges the
                                       # control plane (409s all runs/ops)


_TAIL_LINES = 40


def _watch_local(rec: dict, proc: subprocess.Popen) -> None:
    global _child, _child_run
    with _lock:
        rec["status"] = "running"
    # STREAM the produce's stderr (its structured logs + any traceback) line by line onto THIS process's
    # stderr, so every line reaches the task's CloudWatch stream AS IT HAPPENS (a running produce is
    # visible, not a black box until exit), and keep the tail on the run record so the /run API shows WHY
    # a produce failed. A hung produce is killed at the timeout so it never holds the run slot forever.
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    timed_out = threading.Event()

    def _kill_on_timeout():
        timed_out.set()
        proc.kill()
    timer = threading.Timer(_RUN_TIMEOUT, _kill_on_timeout)
    timer.daemon = True
    timer.start()
    try:
        for line in (proc.stdout if proc.stderr is None else proc.stderr):   # the photos run: one merged stream
            sys.stderr.write(line)
            sys.stderr.flush()
            tail.append(line.rstrip("\n"))
        rc = proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        rc = -1
        log.error("produce run exceeded timeout; killed", extra={"extra_fields": {
            "run_id": rec["run_id"], "timeout_s": _RUN_TIMEOUT}})
        tail.append(f"produce run exceeded timeout ({_RUN_TIMEOUT}s); killed")
    tail_text = "\n".join(tail)
    # ledger totals after a produce; a photos run's own summary (chosen, review, pending, spend)
    counts = _photo_counts(rec) if rec.get("stage") == "photos" else _capture_counts()
    with _lock:
        if _child is proc:
            _child, _child_run = None, None
        rec["finished_at"] = _now()
        rec["counts"] = counts
        # it gave way: stopped by us for another run (not a failure, its work is kept); a run that ended on its own
        # before the signal keeps its real status
        if rec.get("preempted_by") and rec.get("signalled") and rc != 0:
            rec["status"] = "stopped"
            rec["error"] = f"stopped for the {rec['preempted_by']}; its work is kept for the next photos run"
        else:
            rec["status"] = "succeeded" if rc == 0 else "failed"
            if rc != 0:
                rec["error"] = f"pipeline exited {rc}" + (f":\n{tail_text}" if tail_text else "")
    # the produce subprocess owns persisting the scrape trees and publishing the deliverables (its exit
    # code carries a publish failure, with the cause in the streamed tail above)
    _stamp_last_run(rec, rec["status"])
    _persist_run(rec)                                   # durable `last` across a restart
    # An inventory run is a stock refresh, NOT a validated produce, so it must not advance the admission
    # consistency streak nor overwrite the latest full-run diagnostic (matches produce._finalize_control_plane).
    # Neither is a photos run (its own republish finalizes the control plane inside produce).
    if rec.get("stage") not in ("inventory", "photos"):
        _persist_diagnostics()                          # per-source layer diagnostics -> config.db (for :4200)
        _evaluate_admission()                           # record consistency history + auto-demote on drift
    log.info("scraper run finished", extra={"extra_fields": {"run_id": rec["run_id"], "rc": rc}})
    _follow_up(rec)


def _follow_up(rec: dict) -> None:
    """After a full produce (stage all), the photos run (with STANDARDISE.AUTO on): its own run, on the sheet this produce
    just published. A run slot taken in between (an op claimed it) is logged; the next produce starts it again."""
    from stone_pipeline.config.settings import STANDARDISE
    if _stopping or not (STANDARDISE.AUTO and rec.get("status") == "succeeded" and rec.get("stage") in _PHOTOS_AFTER):
        return
    follow, code = start_run(stage="photos")
    if code != 202:
        log.error("photos run not started after the produce", extra={"extra_fields": {
            "after": rec["run_id"], "status": code, "error": follow.get("error")}})


def _build_command(rec: dict) -> list[str]:
    """The subprocess for this run. A photos run is the standardised-photo process (unbuffered: its report is its
    stdout). Every other stage is a produce: FULL produce = stone_pipeline.produce (fetch export inputs
    -> LIVE scrape -> build), NOT bare `stone_pipeline.build`: build assumes data/ is already scraped
    (the laptop path), so on a fresh host (a new ECS task with empty data/) it would produce nothing.
    produce guarantees the inputs first. `--stage` scopes HOW FAR (scrape / catalog / all) and
    `--sources` scopes WHICH scrapers (omitted -> every enabled source)."""
    if rec.get("stage") == "photos":
        return [sys.executable, "-u", "-m", "deploy.standardise_run"]
    cmd = [sys.executable, "-m", "stone_pipeline.produce", "--stage", rec.get("stage", "all")]
    if rec.get("scope"):
        cmd += ["--sources", ",".join(rec["scope"])]
    return cmd


def _summary_path(rec: dict) -> Path:
    """Where a photos run hands its summary back (SCRAPER_RUN_SUMMARY) for its run record."""
    return Path(tempfile.gettempdir()) / f"photos-{rec['run_id']}.json"


def _photo_counts(rec: dict) -> dict | None:
    """A photos run's summary (chosen, review, pending, spend, ...), read once and removed; None when the process ended
    without one (a crash: its tail says why)."""
    path = _summary_path(rec)
    try:
        counts = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    path.unlink()
    return counts


def _launch_local(rec: dict) -> None:
    """Start the run's subprocess and its watcher. Under _lock, in one section: whether the run may still start (it was
    not marked to give way, the server is not stopping) and the child's registration. A give-way mark and stop_current
    take the same lock, so a run is either registered first (and then found and stopped) or sees the mark and never
    starts; two runs can never be under way at once."""
    global _child, _child_run, _watcher
    photos = rec.get("stage") == "photos"
    with _lock:
        refused = ("the service is stopping" if _stopping
                   else f"it gave way to the {rec['preempted_by']}" if rec.get("preempted_by") else None)
        if refused is None:
            proc = subprocess.Popen(
                _build_command(rec),
                # the run id reaches the deliverables manifest produce publishes; a photos run hands its summary back
                # and is told whether it is close-only by its own run alone, never by the service's env
                env={**os.environ, "SCRAPER_LEDGER_WRITETHROUGH": "1", "SCRAPER_RUN_ID": rec["run_id"],
                     **({"SCRAPER_RUN_SUMMARY": str(_summary_path(rec)),
                         "SCRAPER_STANDARDISE_CLOSE_ONLY": "true" if rec["close_only"] else "false"} if photos else {})},
                # A produce's stdout is the image-progress print() noise (drop it); its stderr carries the structured
                # logfmt logs + any traceback, so CAPTURE it (see _watch_local) instead of discarding a failed run's
                # cause. The photo process reports on stdout, so its two streams are captured as one.
                stdout=subprocess.PIPE if photos else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if photos else subprocess.PIPE, text=True)
            watcher = threading.Thread(target=_watch_local, args=(rec, proc), daemon=True)
            _child, _child_run, _watcher = proc, rec, watcher
    if refused is not None:
        _not_started(rec, refused)
        return
    watcher.start()


def _not_started(rec: dict, why: str) -> None:
    """A run that never started: one that gave way is stopped (its work is kept), one the stopping server refused failed
    (press again once the service is back)."""
    with _lock:
        rec["status"] = "stopped" if rec.get("preempted_by") else "failed"
        rec["finished_at"] = _now()
        rec["error"] = f"not started: {why}"
    _stamp_last_run(rec, rec["status"])
    _persist_run(rec)
    log.info("run not started", extra={"extra_fields": {"run_id": rec["run_id"], "why": why}})


def _reap_photos(run_id: str, deadline: float, clock=time.monotonic) -> None:
    """Clear what the killed photos run `run_id` left: its GPU jobs and, while still its own, the pool's ceiling and its
    run lock (standardise_run.reap). In its own thread, waited for until the deadline at most: the run that takes the
    slot is never held past it, and the clearing finishes on its own. Its modules are imported only on this rare path.
    A failure is logged and goes no further: the next photos run clears the same things (clear_leftovers, the stale
    lock taken over)."""
    def clear():
        try:
            from deploy import standardise_run
            standardise_run.reap(run_id)
        except Exception:
            log.exception("could not clear what the killed photos run left; the next photos run clears it")
    clearing = threading.Thread(target=clear, daemon=True)
    clearing.start()
    clearing.join(timeout=max(0.0, deadline - clock()))
    if clearing.is_alive():
        log.warning("clearing the killed photos run goes on past the deadline", extra={"extra_fields": {"run_id": run_id}})


def _end_child(deadline: float, clock=time.monotonic, reap: bool = False) -> None:
    """End the running run's subprocess by `deadline` (on `clock`): SIGTERM, the time left to clean up (the photo process
    stops its GPU jobs, sets the pool's ceiling back, keeps its spend and releases its lock), then killed; with `reap`, a
    killed photos run's leftovers are cleared within the deadline too; then the time left for its watcher to finish
    the run record."""
    with _lock:
        child, run, watcher = _child, _child_run, _watcher
        signal_it = child is not None and child.poll() is None
        if signal_it and run is not None:
            run["signalled"] = True
    if signal_it:
        child.terminate()
        try:
            child.wait(timeout=max(0.0, deadline - clock()))
        except subprocess.TimeoutExpired:
            log.error("the running run did not end within the stop grace; killed")
            child.kill()
            if reap and run is not None and run.get("stage") == "photos":
                _reap_photos(run["run_id"], deadline, clock)
    if watcher is not None:
        watcher.join(timeout=max(0.0, deadline - clock()))


def stop_current(clock=time.monotonic) -> None:
    """The service is stopping (ECS sent SIGTERM and kills the task after its stopTimeout): no run starts any more, and
    the running run ends (_end_child) within RUN_STOP_GRACE_SECONDS, which leaves the rest of the stopTimeout for the
    server's last config save. What a killed photos run left is not cleared here (it could eat that save's time): the
    next photos run clears it."""
    global _stopping
    with _lock:
        _stopping = True
    _end_child(clock() + RUN_STOP_GRACE_SECONDS, clock)


def _launch(rec: dict, launch) -> tuple[dict, int]:
    """Start the run's subprocess: (record, 202), or (record, 500) for a launch that failed (a completed, failed run)."""
    try:
        (launch or _launch_local)(rec)
    except Exception as exc:
        with _lock:
            rec["status"] = "failed"
            rec["finished_at"] = _now()
            rec["error"] = str(exc)
        _stamp_last_run(rec, "failed")
        _persist_run(rec)
        log.exception("scraper run failed to launch")
        return _public(rec), 500
    return _public(rec), 202


def _give_way_then_launch(rec: dict, launch) -> None:
    """The photos run that held the slot gives way: ended (or, not launched yet, it never starts), then this run starts."""
    _end_child(time.monotonic() + RUN_STOP_GRACE_SECONDS, reap=True)
    _launch(rec, launch)


# -- public API ---------------------------------------------------------------

def start_run(sources=None, stage="all", launch=None, close_only=False) -> tuple[dict, int]:
    """Kick off a produce. `sources` None -> every enabled source, else an explicit subset; `stage`
    is scrape / catalog / all (how far to run). Returns (record, http_status): 202 started, 409 if a
    run is already in flight, 400 on a bad stage. `scope` (the explicit subset, internal) is what
    actually scopes the subprocess; `sources` is the resolved list shown to the caller. `close_only`
    (photos only, an operator's action): the photos run makes the chosen photos' close-ups again
    instead of picking and judging (STANDARDISE_PROCESS.md)."""
    stage = (stage or "all").strip().lower()
    if stage not in STAGES:
        return {"error": f"unknown stage {stage!r}; expected one of {', '.join(STAGES)}"}, 400
    if stage == "photos" and sources:
        return {"error": "the photos stage works on every listing without a chosen photo; it takes no sources"}, 400
    if not isinstance(close_only, bool):
        return {"error": f"close_only must be true or false; got {close_only!r}"}, 400
    if close_only and stage != "photos":
        return {"error": f"close_only is a photos run's; the {stage} stage takes none"}, 400
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
        held = _runs.get(_current_id) if _current_id else None
        if held and held["status"] not in ("queued", "running"):
            held = None
        # a photos run gives way to any other stage (the owner's Publish is never blocked); anything else is refused
        if held and (held.get("stage") != "photos" or stage == "photos"):
            return _public(held), 409
        run_id = _now().translate({ord(c): None for c in ":-.T"})[:17]
        while run_id in _runs:                     # a claim within the same millisecond (a follow-up, a Publish at once)
            run_id = str(int(run_id) + 1)          # takes the next one: a run id names one run
        if held:                                   # marked with the claim, under the lock its launch checks
            held["preempted_by"] = f"{stage} run {run_id}"
        # a photos run is the environment's, no source's: it stamps no source's 'last run'
        shown = [] if stage == "photos" else scope if scope is not None else _resolve_sources(None)
        rec = {"run_id": run_id, "status": "queued",
               "started_at": _now(), "finished_at": None, "error": None,
               "sources": shown,
               "stage": stage, "scope": scope, "close_only": close_only, "progress": {}}
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
            if held:                                # the photos run that was to give way keeps the slot and runs on
                held.pop("preempted_by")
            _current_id = held["run_id"] if held else None
        return {"error": f"a {busy} is in progress; refusing to run"}, 409
    _stamp_last_run(rec, "running")                # list shows this source as running immediately
    if held:
        threading.Thread(target=_give_way_then_launch, args=(rec, launch), daemon=True).start()
        return _public(rec), 202
    return _launch(rec, launch)


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
