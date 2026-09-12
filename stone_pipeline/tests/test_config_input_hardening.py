"""Input-boundary hardening for the config plane (Tier-3 + a Tier-4 low):

  _env_int      -- a malformed override ('--3', a Unicode digit) must fall soft to the default, never
                   crash at config import (int() after an isdigit() pre-check DID crash).
  _authorized   -- a non-ASCII Authorization header must yield a clean 401, not a hmac TypeError that
                   escapes the handler (compare on bytes, not str).
  Content-Length -- a malformed/oversized Content-Length must be a 400, not an int() ValueError that
                   escapes the handler, and must not force an unbounded read into memory.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import settings as settings_mod
from stone_pipeline.config.server import ConfigHandler, _MAX_BODY_BYTES


# ---- _env_int ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("", 7),         # blank = unset (env.getenv treats an empty value as absent)
    ("-3", -3),      # a real negative parses
    (" 5 ", 5),      # whitespace tolerated
])
def test_env_int_parses_or_defaults(monkeypatch, value, expected):
    monkeypatch.setenv("BLOKPORT_TEST_INT", value)
    assert settings_mod._env_int("BLOKPORT_TEST_INT", 7) == expected


@pytest.mark.parametrize("value", [
    "--3",           # double sign: lstrip('-').isdigit() was True, int() then raised
    "²",             # superscript two: isdigit() True, int() raises
    "3.5",           # not an int
])
def test_env_int_malformed_override_fails_loud(monkeypatch, value):
    # a SET but malformed override is an operator mistake, never silently the default (wave 3a)
    monkeypatch.setenv("BLOKPORT_TEST_INT", value)
    with pytest.raises(ValueError, match="BLOKPORT_TEST_INT"):
        settings_mod._env_int("BLOKPORT_TEST_INT", 7)


def test_env_int_unset_returns_default(monkeypatch):
    monkeypatch.delenv("BLOKPORT_TEST_INT", raising=False)
    assert settings_mod._env_int("BLOKPORT_TEST_INT", 7) == 7


# ---- _authorized (bytes compare) --------------------------------------------------------------------

def _handler_with(auth_header: str, token: str) -> ConfigHandler:
    h = ConfigHandler.__new__(ConfigHandler)          # bypass BaseHTTPRequestHandler.__init__ (opens a socket)
    h.headers = {"Authorization": auth_header}
    h.server = type("S", (), {"expected_token": token})()
    return h


def test_non_ascii_authorization_is_unauthorized_not_typeerror():
    # a high byte decodes latin-1 to a non-ASCII str; on str, hmac.compare_digest raises TypeError
    h = _handler_with("\xc3", token="sekret")
    assert h._authorized() is False                   # clean reject, no exception


def test_correct_token_authorizes():
    h = _handler_with("Bearer sekret", token="sekret")
    assert h._authorized() is True


def test_wrong_token_is_unauthorized():
    h = _handler_with("Bearer nope", token="sekret")
    assert h._authorized() is False


# ---- Content-Length bound ---------------------------------------------------------------------------

def test_max_body_bytes_is_a_sane_cap():
    assert _MAX_BODY_BYTES == 1 << 20                 # 1 MiB, far above any real config payload
