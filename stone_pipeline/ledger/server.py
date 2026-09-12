"""HTTP surface for the sync service (SYNC_LEDGER_DESIGN.md section 8.2, 10).

A thin, dependency-free reference server that maps the versioned endpoints to the
sync engine, so Medusa's pull job can talk to the ledger and ack ids back:

    GET  /sync/v1/status
    GET  /sync/v1/failures                       drill-down behind the status gap_held count
    GET  /sync/v1/<type>?status=ready&limit=N   type in {variations, products, inventory, removed}
    POST /sync/v1/ack   body: [{type, external_id, medusa_id, status}, ...]
      (removed acks use status 'done' -> retire, or 'blocked' -> keep + retry, dead-letter after N)
      LOST-UPDATE GUARD (optional, strongly recommended): echo back the version that was APPLIED so the
      ledger only marks 'synced' if it is still current -- otherwise a produce that changed the row between
      the pull and the ack is silently lost. For variations/products add `payload_hash` (the value served
      on the pull item); for inventory add `quantity` (the served payload.quantity). Backward compatible:
      if omitted, the prior behavior is kept, so the guard engages the moment the consumer starts echoing.
    POST /sync/v1/requeue   body (optional): {type: variations|products}  -- un-quarantine gap_held
      entities back to dirty after the Medusa-side cause is fixed (the recovery lever behind failures)
    POST /sync/v1/abandon   body: {type: variations|products|removed, external_id}  -- the OPPOSITE of
      requeue: drop ONE structurally-re-rejecting dead-letter to terminal (abandoned_at). Keyed by the
      same {type, external_id} a /failures row carries. 404 unknown id, 409 if not a dead-letter.

Auth is a bearer token (BLOKPORT_SYNC_TOKEN), matching the per-env SSM secret the
design calls for; the server refuses to start without it. This is the contract
surface and is fine for dev and behind-the-VPC use; a production deployment fronts
it with TLS and a real ASGI server, but the routes and semantics are these.

Routing is split from transport (`dispatch` is a pure function) so it is testable
without sockets. No em dashes (design principle 2).
"""

from __future__ import annotations

import hmac
import json
import os
from stone_pipeline.core import env
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from stone_pipeline.core import logfmt
from stone_pipeline.ledger import snapshot, sync, writethrough
from stone_pipeline.ledger.db import Ledger

log = logfmt.get_logger("ledger.server")

# Cap an authed request body so a bad/hostile Content-Length can't force an unbounded read into memory.
# The largest real body is an ack page (2000 items, well under 1 MiB); 8 MiB is far above it.
_MAX_BODY_BYTES = 8 << 20
# Page bounds for the pull routes. An unbounded pull on a full delta builds the whole set in memory and
# writes it in one buffer (http.server has no backpressure): a 24k-item bootstrap delta is ~4.6 MB.
_DEFAULT_PAGE, _MAX_PAGE = 500, 2000
_DEFAULT_FAILURES_PAGE = 200


def _page_limit(query: dict[str, list[str]], default: int) -> int | None:
    """?limit= as an int: absent -> `default`; above the cap -> the cap (the page stays bounded, and the
    response carries what was served); not a positive integer -> None (the caller answers 400). Never a
    silent fall-through to the default, which hid a client's typo as a short page."""
    raw = (query.get("limit") or [""])[0]
    if not raw:
        return default
    if not raw.isdigit() or int(raw) == 0:
        return None
    return min(int(raw), _MAX_PAGE)


def dispatch(ledger: Ledger, method: str, resource: str,
             query: dict[str, list[str]], body) -> tuple[int, object]:
    """Route one request to the sync engine. Pure: returns (status_code, json body).

    `resource` is the last path segment (status, variations, products, ack)."""
    if method == "GET" and resource == "status":
        return 200, sync.status(ledger)
    if method == "GET" and resource == "failures":
        # drill-down behind the status gap_held count: what Medusa rejected and why.
        if (limit := _page_limit(query, _DEFAULT_FAILURES_PAGE)) is None:
            return 400, {"error": "limit must be a positive integer"}
        return 200, {"failures": sync.failures(ledger, limit)}
    if method == "GET" and resource in ("variations", "products", "inventory", "removed"):
        if (limit := _page_limit(query, _DEFAULT_PAGE)) is None:
            return 400, {"error": "limit must be a positive integer"}
        return 200, {"type": resource, "items": sync.ready(ledger, resource, limit)}
    if method == "POST" and resource == "ack":
        if not isinstance(body, list):
            return 400, {"error": "ack body must be a JSON list of acks"}
        result = sync.ack_batch(ledger, body)
        return 200, {"acked": result["applied"], "missed": result["missed"], "skipped": result["skipped"]}
    if method == "POST" and resource == "requeue":
        # recovery lever: un-quarantine dead-lettered ('gap_held') entities back to 'dirty' so they
        # re-serve, after the underlying Medusa issue is fixed. Body (optional): {"type":
        # "variations"|"products"}; omitted -> both. Observed via GET /sync/v1/failures + /status.
        type_ = body.get("type") if isinstance(body, dict) else None
        if type_ is not None and type_ not in ("variations", "products"):
            return 400, {"error": "type must be 'variations' or 'products' (or omitted for both)"}
        return 200, {"requeued": sync.requeue_dead_lettered(ledger, type_=type_)}
    if method == "POST" and resource == "abandon":
        # the opposite of requeue: drop ONE structurally-re-rejecting dead-letter to a terminal
        # `abandoned_at` -- it stops re-serving, requeue no longer resurrects it, and it still renders in
        # GET /sync/v1/failures tagged abandoned. Keyed by the SAME {type, external_id} a failures row
        # carries, so a per-row 'Abandon' button passes it straight through. A reset un-abandons it.
        type_ = body.get("type") if isinstance(body, dict) else None
        xid = body.get("external_id") if isinstance(body, dict) else None
        if not type_ or not xid:
            return 400, {"error": "abandon body must be {type, external_id}"}
        result = sync.abandon_dead_letter(ledger, type_, xid)
        if "error" in result and "unknown type" in result["error"]:
            return 400, result
        if result.get("found") is False:
            return 404, {"error": f"unknown {type_} {xid!r}"}
        if result.get("abandoned") is False:                 # not a dead-letter (still live/serving)
            return 409, result
        return 200, result
    return 404, {"error": f"no route for {method} /sync/{resource}"}


def _expected_token() -> str:
    token = env.getenv("BLOKPORT_SYNC_TOKEN", "").strip()
    if not token:
        raise SystemExit("BLOKPORT_SYNC_TOKEN is not set; refusing to start the sync server")
    return token


class SyncHandler(BaseHTTPRequestHandler):
    server_version = "scraper-sync/1.0"   # brand-neutral banner (one image serves every brand)

    def _respond(self, code: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        # constant-time compare so the token can't be recovered by response-timing (L1). On BYTES: the header
        # value is client-controlled (latin-1 decoded, so a high byte yields a non-ASCII str) and compare_digest
        # raises TypeError on a non-ASCII str, which escapes this pre-dispatch check and drops the connection
        # with a traceback instead of a clean 401. utf-8 encoding always succeeds.
        header = self.headers.get("Authorization", "").encode("utf-8")
        expected = f"Bearer {self.server.expected_token}".encode("utf-8")  # type: ignore[attr-defined]
        return hmac.compare_digest(header, expected)

    def _handle(self, method: str) -> None:
        if not self._authorized():
            return self._respond(401, {"error": "unauthorized"})
        parts = urlsplit(self.path)
        segments = [s for s in parts.path.split("/") if s]
        # versioned: /sync/v1/<resource> (two independently-evolving systems need a boundary)
        if len(segments) != 3 or segments[0] != "sync" or segments[1] != "v1":
            return self._respond(404, {"error": "not found; expected /sync/v1/<resource>"})
        resource = segments[2]
        body = None
        if method == "POST":
            # A malformed Content-Length must be a 400, not an int() ValueError that escapes the handler (this
            # parse sits BEFORE the dispatch try/except) and drops the connection; the length is client-
            # controlled, so cap it before read() to bound memory.
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._respond(400, {"error": "invalid Content-Length"})
            if length < 0 or length > _MAX_BODY_BYTES:
                return self._respond(400, {"error": f"body too large (max {_MAX_BODY_BYTES} bytes)"})
            try:
                body = json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                return self._respond(400, {"error": "invalid JSON body"})
        try:
            with Ledger.open(self.server.ledger_path, env=writethrough.ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint()) as ledger:  # type: ignore[attr-defined]
                code, payload = dispatch(ledger, method, resource, parse_qs(parts.query), body)
        except Exception:
            log.exception("sync request failed", extra={"extra_fields": {"path": self.path}})
            return self._respond(500, {"error": "internal error"})
        self._respond(code, payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def log_message(self, *args) -> None:  # route access logs through structured logging
        log.info("sync request", extra={"extra_fields": {"client": self.address_string(),
                                                          "request": args[0] % args[1:] if args else ""}})


def bootstrap_ledger_if_missing(path) -> None:
    """Create (and best-effort seed) the ledger when it does not exist, so the sync server is
    self-sufficient on a fresh host (e.g. ECS on a new EFS volume) instead of refusing to start.
    Seeds the id foundation when the exports are present; otherwise leaves an empty ledger that the
    first produce populates. Idempotent: a no-op once the ledger exists. Built beside the path and
    renamed into place: the config container awaits the path, so it must only ever appear complete."""
    if path.exists():
        return
    log.info("no ledger yet; bootstrapping", extra={"extra_fields": {"ledger": str(path)}})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.bootstrap.tmp")
    tmp.unlink(missing_ok=True)   # a bootstrap the previous task died in
    try:
        with writethrough.open_ledger(tmp):   # create schema + seed; __exit__ commits
            pass
    except Exception:
        log.exception("ledger seed skipped; starting empty (a produce will populate it)")
        tmp.unlink(missing_ok=True)
        Ledger.open(tmp, env=writethrough.ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint()).close()
    os.replace(tmp, path)


def shutdown(path, periodic) -> None:
    """SIGTERM path (ECS sends SIGTERM before stopping the task): stop the periodic snapshot thread and
    wait for an in-flight save, THEN save the ledger once, then exit 0. Same order as the config server,
    so the final save never races interpreter teardown."""
    if hasattr(periodic, "stop"):
        periodic.stop()
    else:
        periodic.set()
    snapshot.save(path)
    raise SystemExit(0)


def serve(host: str | None = None, port: int = 8723) -> None:
    # default 127.0.0.1 (safe on a laptop); ECS sets BLOKPORT_BIND_HOST=0.0.0.0 so Medusa (over the
    # VPC) can reach it. The bearer token still gates every request.
    host = host or env.getenv("BLOKPORT_BIND_HOST", "127.0.0.1")
    path = writethrough.ledger_path()
    # C1: the ledger is on LOCAL (ephemeral) disk. On a cold task, restore the last S3 snapshot BEFORE
    # anything can create a fresh empty file (which would lose the acked ids); only then seed if still
    # absent. This server owns the periodic + on-stop snapshot (it shares the local volume with config).
    snapshot.restore(path, required=True)   # durable system-of-record: fail loud if a present snapshot won't fetch
    bootstrap_ledger_if_missing(path)
    periodic = snapshot.start_periodic(path)
    signal.signal(signal.SIGTERM, lambda *_: shutdown(path, periodic))
    httpd = ThreadingHTTPServer((host, port), SyncHandler)
    httpd.expected_token = _expected_token()   # type: ignore[attr-defined]
    httpd.ledger_path = path                   # type: ignore[attr-defined]
    log.info("sync server listening", extra={"extra_fields": {"host": host, "port": port,
                                                              "ledger": str(path)}})
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
