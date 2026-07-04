"""HTTP surface for the sync service (SYNC_LEDGER_DESIGN.md section 8.2, 10).

A thin, dependency-free reference server that maps the versioned endpoints to the
sync engine, so Medusa's pull job can talk to the ledger and ack ids back:

    GET  /sync/v1/status
    GET  /sync/v1/<type>?status=ready&limit=N   type in {variations, products, inventory, removed}
    POST /sync/v1/ack   body: [{type, external_id, medusa_id, status}, ...]
      (removed acks use status 'done' -> retire, or 'blocked' -> keep + retry, dead-letter after N)

Auth is a bearer token (BLOKPORT_SYNC_TOKEN), matching the per-env SSM secret the
design calls for; the server refuses to start without it. This is the contract
surface and is fine for dev and behind-the-VPC use; a production deployment fronts
it with TLS and a real ASGI server, but the routes and semantics are these.

Routing is split from transport (`dispatch` is a pure function) so it is testable
without sockets. No em dashes (design principle 2).
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from stone_pipeline.core import logfmt
from stone_pipeline.ledger import sync, writethrough
from stone_pipeline.ledger.db import Ledger

log = logfmt.get_logger("ledger.server")


def dispatch(ledger: Ledger, method: str, resource: str,
             query: dict[str, list[str]], body) -> tuple[int, object]:
    """Route one request to the sync engine. Pure: returns (status_code, json body).

    `resource` is the last path segment (status, variations, products, ack)."""
    if method == "GET" and resource == "status":
        return 200, sync.status(ledger)
    if method == "GET" and resource == "failures":
        # drill-down behind the status gap_held count: what Medusa rejected and why.
        return 200, {"failures": sync.failures(ledger)}
    if method == "GET" and resource in ("variations", "products", "inventory", "removed"):
        # bound every page. An unbounded pull on a full delta builds the whole set in
        # memory and writes it in one buffer (http.server has no backpressure): a
        # 24k-item bootstrap delta is ~4.6 MB in a single response. Default + cap the page.
        raw = (query.get("limit") or [""])[0]
        default_page, max_page = 500, 2000
        limit = int(raw) if raw.isdigit() and 0 < int(raw) <= max_page else default_page
        return 200, {"type": resource, "items": sync.ready(ledger, resource, limit)}
    if method == "POST" and resource == "ack":
        if not isinstance(body, list):
            return 400, {"error": "ack body must be a JSON list of acks"}
        result = sync.ack_batch(ledger, body)
        return 200, {"acked": result["applied"], "missed": result["missed"], "skipped": result["skipped"]}
    return 404, {"error": f"no route for {method} /sync/{resource}"}


def _expected_token() -> str:
    token = os.environ.get("BLOKPORT_SYNC_TOKEN", "").strip()
    if not token:
        raise SystemExit("BLOKPORT_SYNC_TOKEN is not set; refusing to start the sync server")
    return token


class SyncHandler(BaseHTTPRequestHandler):
    server_version = "blokport-sync/1.0"

    def _respond(self, code: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.server.expected_token}"  # type: ignore[attr-defined]

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
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                return self._respond(400, {"error": "invalid JSON body"})
        try:
            with Ledger.open(self.server.ledger_path, env=writethrough.ENV_NAME) as ledger:  # type: ignore[attr-defined]
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


def serve(host: str | None = None, port: int = 8723) -> None:
    # default 127.0.0.1 (safe on a laptop); ECS sets BLOKPORT_BIND_HOST=0.0.0.0 so Medusa (over the
    # VPC) can reach it. The bearer token still gates every request.
    host = host or os.environ.get("BLOKPORT_BIND_HOST", "127.0.0.1")
    path = writethrough.ledger_path()
    if not path.exists():
        # Fresh host (e.g. ECS on a new EFS volume): create the ledger so the server is self-sufficient
        # instead of refusing to start. Seed the id foundation when the exports are present
        # (best-effort); otherwise start empty and the first produce populates it.
        log.info("no ledger yet; bootstrapping", extra={"extra_fields": {"ledger": str(path)}})
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with writethrough.open_ledger():   # create schema + seed; __exit__ commits
                pass
        except Exception:
            log.exception("ledger seed skipped; starting empty (a produce will populate it)")
            Ledger.open(path, env=writethrough.ENV_NAME).close()   # ensure the file exists to serve
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
