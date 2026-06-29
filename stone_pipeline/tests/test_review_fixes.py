"""Regression tests for the code-review fixes.

Locks in: EU multi-dot number parsing, the SSRF guard (previously zero coverage), and clean.py's
marker-aware scrape retention (never delete the complete folder the pipeline still ingests).
"""

from __future__ import annotations

import csv
import json
import socket

from stone_pipeline import catalog, clean
from stone_pipeline.core.numbers import parse_number
from stone_pipeline.io import ssrf
from stone_pipeline.matching.engine import VariationEngine
from stone_pipeline.matching.index import CandidateIndex
from stone_pipeline.stages import emit


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


# --- engine: genuine duplicate-canonical ambiguity routes to review ----------
def _engine(*candidates):
    idx = CandidateIndex()
    for cid, canonical, surfaces, btype in candidates:
        idx.add(cid, canonical, surfaces=surfaces, block_type=btype)
    return VariationEngine(idx, auto_accept=92, review_floor=84)

def test_duplicate_canonical_no_block_routes_to_review():
    # two DISTINCT variants with the IDENTICAL canonical name and no block info to separate them ->
    # ambiguous; must route to review, not auto-accept an arbitrary one by export-row order.
    eng = _engine(("v1", "Imperial White", ["Imperial White"], "granite"),
                  ("v2", "Imperial White", ["Imperial White"], "quartzite"))
    m = eng.match("Imperial White")            # no block_type -> can't disambiguate
    assert m.cid is None and "ambiguous" in m.method

def test_duplicate_canonical_block_disambiguates():
    eng = _engine(("v1", "Imperial White", ["Imperial White"], "granite"),
                  ("v2", "Imperial White", ["Imperial White"], "quartzite"))
    m = eng.match("Imperial White", block_type="granite")   # block narrows to one
    assert m.cid == "v1"

def test_shared_alias_different_canonicals_still_resolves():
    # two variants share an ALIAS token but have DIFFERENT canonicals -> NOT ambiguous; resolves.
    eng = _engine(("v1", "Arabescato Garfagnana", ["Arabescato Garfagnana", "Arabescato"], "marble"),
                  ("v2", "Arabescato Cervaiole", ["Arabescato Cervaiole", "Arabescato"], "marble"))
    assert eng.match("Arabescato Garfagnana").cid == "v1"    # exact canonical
    assert eng.match("Arabescato", block_type="marble").cid in ("v1", "v2")  # shared alias -> still resolves

def test_fuzzy_match_is_order_independent_within_run():
    # the #6 fix: a non-exact (fuzzy) match must NOT add a live alias mid-run, so a SECOND identical
    # query resolves the SAME way -- not promoted to 'exact'(high) purely because it came later.
    eng = _engine(("v1", "Bianco Carrara", ["Bianco Carrara"], "marble"))
    first = eng.match("Bianco Carara")     # typo -> fuzzy/near-miss
    second = eng.match("Bianco Carara")    # same query again, later in the batch
    assert first.cid == second.cid == "v1"
    assert first.method == second.method   # deterministic: both fuzzy, not first-fuzzy/second-exact


def test_split_bracket_alias():
    # a '(alternative)' in a variety Name moves to the alias list: 'Black Galaxy (Star Galaxy)' ->
    # name 'Black Galaxy' + alias 'Star Galaxy'.
    from stone_pipeline.core.text import split_bracket_alias
    assert split_bracket_alias("Black Galaxy (Star Galaxy)") == ("Black Galaxy", ["Star Galaxy"])
    assert split_bracket_alias("Verde Ubatuba (Ubatuba)") == ("Verde Ubatuba", ["Ubatuba"])
    assert split_bracket_alias("Carrara [Bianco Carrara]") == ("Carrara", ["Bianco Carrara"])
    assert split_bracket_alias("Plain Name") == ("Plain Name", [])      # untouched, no alias
    assert split_bracket_alias("A (x) B (y)") == ("A B", ["x", "y"])    # multiple brackets


# --- consolidate_inventory: cross-source SKU collision, newest wins ----------
def _write_inv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(emit.INVENTORY_COLUMNS)
        w.writerows(rows)

def test_consolidate_inventory_cross_source_case_insensitive_last_wins(tmp_path):
    # two DIFFERENT sources' files carrying the SAME sku in different case must collapse to one row,
    # with the later (newest-run) file's quantity winning.
    a = tmp_path / "runA" / "inventory_update.csv"
    b = tmp_path / "runB" / "inventory_update.csv"
    _write_inv(a, [["zuc-1", "h", "Default", "5", ""]])
    _write_inv(b, [["ZUC-1", "h", "Default", "9", ""]])     # same SKU, uppercased, newer
    out = tmp_path / "to_upload"
    n = catalog.consolidate_inventory([a, b], to_upload=out)
    rows = list(csv.DictReader((out / "4_inventory_update.csv").open(encoding="utf-8-sig")))
    assert n == 1                                            # collapsed case-insensitively
    assert rows[0]["Inventory Quantity"] == "9"             # last file (newest) wins
