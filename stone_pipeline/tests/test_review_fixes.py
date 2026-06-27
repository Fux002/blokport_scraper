"""Regression tests for the code-review fixes.

Locks in: EU multi-dot number parsing, the SSRF guard (previously zero coverage), and clean.py's
marker-aware scrape retention (never delete the complete folder the pipeline still ingests).
"""

from __future__ import annotations

import json
import socket

from stone_pipeline import clean
from stone_pipeline.core.numbers import parse_number
from stone_pipeline.io import ssrf


# --- numbers: EU thousands grouping with >1 dot (was -> None) ----------------
def test_eu_multi_dot_integer():
    assert parse_number("2.500.000") == 2_500_000
    assert parse_number("1.234.567") == 1_234_567

def test_single_dot_stays_decimal():
    assert parse_number("2.80") == 2.8          # unchanged: single dot = decimal
    assert parse_number("1.234") == 1.234       # documented default preserved

def test_mixed_separators_unchanged():
    assert parse_number("1.234,56") == 1234.56  # EU decimal
    assert parse_number("1,234.56") == 1234.56  # US decimal
    assert parse_number("1,234") == 1234        # comma grouping


# --- SSRF guard (io/ssrf.url_allowed) ----------------------------------------
def _resolve_to(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", (ip, 0))])

def test_ssrf_blocks_fargate_metadata(monkeypatch):
    _resolve_to(monkeypatch, "169.254.170.2")          # the credential-stealing target
    assert ssrf.url_allowed("http://evil.example/x") is False

def test_ssrf_blocks_loopback_and_private(monkeypatch):
    for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "::1"):
        _resolve_to(monkeypatch, ip)
        assert ssrf.url_allowed("http://host.example/x") is False, ip

def test_ssrf_allows_public(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    assert ssrf.url_allowed("https://example.com/img.png") is True

def test_ssrf_blocks_non_http_schemes():
    assert ssrf.url_allowed("file:///etc/passwd") is False
    assert ssrf.url_allowed("ftp://host/x") is False
    assert ssrf.url_allowed("gopher://host/x") is False

def test_ssrf_blocks_unresolvable(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("name or service not known")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert ssrf.url_allowed("http://does-not-resolve.example/x") is False

def test_ssrf_blocks_mixed_public_and_private(monkeypatch):
    # a host resolving to BOTH a public and an internal IP must be blocked (all-or-nothing)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda h, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0)),
                                            (2, 1, 6, "", ("169.254.170.2", 0))])
    assert ssrf.url_allowed("http://rebind.example/x") is False


# --- clean.py: keep the complete folder when the newest scrape is incomplete --
def _scrape(folder, complete):
    folder.mkdir(parents=True)
    (folder / "products.csv").write_text("x", encoding="utf-8")
    (folder / "scrape_complete.json").write_text(json.dumps({"complete": complete}), encoding="utf-8")

def test_clean_keeps_authoritative_folder_when_newest_incomplete(tmp_path):
    src = tmp_path / "ferraz"
    _scrape(src / "20260101_000000", True)
    _scrape(src / "20260102_000000", True)
    _scrape(src / "20260103_000000", False)   # newest, but truncated
    stale = {p.name for p in clean._superseded_scrapes(tmp_path)}
    # newest (incomplete) kept for reference + latest COMPLETE (..02) kept (the pipeline uses it);
    # only the older complete one (..01) is superseded.
    assert stale == {"20260101_000000"}

def test_clean_all_complete_keeps_only_newest(tmp_path):
    src = tmp_path / "zucchi"
    _scrape(src / "20260101_000000", True)
    _scrape(src / "20260102_000000", True)
    stale = {p.name for p in clean._superseded_scrapes(tmp_path)}
    assert stale == {"20260101_000000"}        # newest is complete -> it's the only keeper
