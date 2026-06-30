"""HTTP surface for the scraper config store, for the admin UI (e.g. on :4200).

A thin, dependency-free reference server over the config store, so the UI can list
scrapers, toggle which run, and edit per-scraper settings live:

    GET  /config/v1/sources                list every scraper (enabled + settings)
    GET  /config/v1/sources/<name>         one scraper
    PUT  /config/v1/sources/<name>         create or update one (body = the settings)

Auth is a bearer token (BLOKPORT_CONFIG_TOKEN); the server refuses to start without
it. Routing is split from transport (`dispatch` is pure) so it is testable without
sockets. SQLite WAL handles the UI (single writer) plus the pipeline (readers). No
em dashes (design principle 2).
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from stone_pipeline.config import store
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.server")


def dispatch(method: str, segments: list[str], body) -> tuple[int, object]:
    """Route one request. `segments` is the path under /config/v1 (e.g. ['sources']
    or ['sources', 'polonine']). Pure: returns (status_code, json body)."""
    if not segments or segments[0] != "sources":
        return 404, {"error": "not found; expected /config/v1/sources[/<name>]"}
    if len(segments) == 1:
        if method == "GET":
            return 200, {"sources": store.list_rows()}
        return 405, {"error": "use PUT /config/v1/sources/<name> to create"}
    name = segments[1]
    if method == "GET":
        row = store.get_row(name)
        return (200, row) if row else (404, {"error": f"no source {name!r}"})
    if method == "PUT":
        if not isinstance(body, dict):
            return 400, {"error": "body must be a JSON object of source settings"}
        body = {**body, "source": name}   # the path name is authoritative
        store.upsert_row(body)
        return 200, store.get_row(name)
    return 405, {"error": f"method {method} not allowed on a source"}


def _expected_token() -> str:
    token = os.environ.get("BLOKPORT_CONFIG_TOKEN", "").strip()
    if not token:
        raise SystemExit("BLOKPORT_CONFIG_TOKEN is not set; refusing to start the config server")
    return token


class ConfigHandler(BaseHTTPRequestHandler):
    server_version = "blokport-config/1.0"

    def _respond(self, code: int, payload: object) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization", "") == f"Bearer {self.server.expected_token}"  # type: ignore[attr-defined]

    def _handle(self, method: str) -> None:
        if not self._authorized():
            return self._respond(401, {"error": "unauthorized"})
        parts = urlsplit(self.path)
        seg = [s for s in parts.path.split("/") if s]
        if len(seg) < 2 or seg[0] != "config" or seg[1] != "v1":
            return self._respond(404, {"error": "not found; expected /config/v1/..."})
        body = None
        if method == "PUT":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                return self._respond(400, {"error": "invalid JSON body"})
        try:
            code, payload = dispatch(method, seg[2:], body)
        except Exception:
            log.exception("config request failed", extra={"extra_fields": {"path": self.path}})
            return self._respond(500, {"error": "internal error"})
        self._respond(code, payload)

    def do_GET(self) -> None:
        self._handle("GET")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def log_message(self, *args) -> None:
        log.info("config request", extra={"extra_fields": {"client": self.address_string()}})


def serve(host: str = "127.0.0.1", port: int = 8724) -> None:
    if not store.config_db_path().exists():
        store.seed_from_yaml()   # first run: seed from the committed yaml
    httpd = ThreadingHTTPServer((host, port), ConfigHandler)
    httpd.expected_token = _expected_token()   # type: ignore[attr-defined]
    log.info("config server listening", extra={"extra_fields": {
        "host": host, "port": port, "db": str(store.config_db_path())}})
    httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
