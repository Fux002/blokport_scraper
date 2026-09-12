"""Input hardening on the sync server (mirrors test_config_input_hardening for the config server):
the bearer compare never raises on a hostile header, a bad Content-Length is a 400 and a huge one is
refused before the read, and a page `limit` is validated (bad -> 400, over the cap -> the cap) instead
of silently falling back to the default. /failures honours ?limit= as INTEGRATION_CONTRACT documents.
"""

from __future__ import annotations

import io

from stone_pipeline.ledger import server, sync
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.server import SyncHandler, _MAX_BODY_BYTES


def _handler(auth_header: str, token: str = "sekret", headers: dict | None = None,
             path: str = "/sync/v1/ack", body: bytes = b"") -> tuple[SyncHandler, list]:
    h = SyncHandler.__new__(SyncHandler)               # bypass BaseHTTPRequestHandler.__init__ (opens a socket)
    h.headers = {"Authorization": auth_header, **(headers or {})}
    h.server = type("S", (), {"expected_token": token, "ledger_path": None})()
    h.path = path
    h.rfile = io.BytesIO(body)
    out: list = []
    h._respond = lambda code, payload: out.append((code, payload))
    return h, out


# ---- _authorized (bytes compare) --------------------------------------------------------------------

def test_non_ascii_authorization_is_unauthorized_not_typeerror():
    h, _ = _handler("\xc3")                            # a high byte decodes latin-1 to a non-ASCII str
    assert h._authorized() is False


def test_correct_token_authorizes_and_wrong_does_not():
    assert _handler("Bearer sekret")[0]._authorized() is True
    assert _handler("Bearer nope")[0]._authorized() is False


# ---- Content-Length bound ---------------------------------------------------------------------------

def test_max_body_bytes_is_a_sane_cap():
    assert _MAX_BODY_BYTES == 8 << 20                  # an ack page of 2000 items is well under 1 MiB


def test_malformed_content_length_is_a_400_not_a_dropped_connection():
    h, out = _handler("Bearer sekret", headers={"Content-Length": "abc"})
    h._handle("POST")
    assert out == [(400, {"error": "invalid Content-Length"})]


def test_oversized_content_length_is_refused_before_the_read():
    h, out = _handler("Bearer sekret", headers={"Content-Length": str(_MAX_BODY_BYTES + 1)})
    h._handle("POST")
    assert out[0][0] == 400 and "too large" in out[0][1]["error"]


# ---- page limit -------------------------------------------------------------------------------------

def _served_limit(monkeypatch, ledger, query):
    seen: list = []
    monkeypatch.setattr(sync, "ready", lambda lg, resource, limit: seen.append(limit) or [])
    code, body = server.dispatch(ledger, "GET", "products", query, None)
    return code, body, seen


def test_bad_limit_is_a_400_not_the_default_page(tmp_path, monkeypatch):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        for raw in ("abc", "0", "-5"):
            code, body, seen = _served_limit(monkeypatch, ledger, {"limit": [raw]})
            assert code == 400 and "limit" in body["error"] and seen == [], raw


def test_limit_absent_is_the_default_and_over_cap_is_the_cap(tmp_path, monkeypatch):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        assert _served_limit(monkeypatch, ledger, {})[2] == [500]
        assert _served_limit(monkeypatch, ledger, {"limit": ["50"]})[2] == [50]
        assert _served_limit(monkeypatch, ledger, {"limit": ["5000"]})[2] == [2000]


def test_failures_honours_the_documented_limit(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(sync, "failures", lambda lg, limit=200: seen.append(limit) or [])
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        assert server.dispatch(ledger, "GET", "failures", {}, None)[0] == 200
        assert server.dispatch(ledger, "GET", "failures", {"limit": ["25"]}, None)[0] == 200
        assert server.dispatch(ledger, "GET", "failures", {"limit": ["x"]}, None)[0] == 400
    assert seen == [200, 25]
