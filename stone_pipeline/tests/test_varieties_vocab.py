"""GET /config/v1/varieties + the alias validator read from the durable LEDGER variation table, NOT the
fetched Medusa export. The export goes empty in the window right after a reset (products cleared, before
the re-pull), which used to empty the alias picker; the ledger keeps all variations across a reset, so the
picker + validator stay populated and agree by construction.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import varieties
from stone_pipeline.ledger import writethrough
from stone_pipeline.ledger.db import Ledger

_NOW = "2026-01-01T00:00:00Z"


def _seed(path, rows):
    with Ledger.open(path, env=writethrough.ENV_NAME) as lg:
        for key, name, typ in rows:
            lg.upsert("variation", {
                "key": key, "branch": key.split("_", 1)[0], "type": typ, "name": name,
                "aliases": "[]", "image_url": "", "image_sha256": None, "image_model": None,
                "volume": "", "medusa_id": None, "payload_hash": "", "state": "pending",
                "first_seen": _NOW, "last_synced": None, "created_at": _NOW, "updated_at": _NOW,
            }, pk=("key",))


@pytest.fixture
def ledger_with_varieties(tmp_path, monkeypatch):
    p = tmp_path / "dev.ledger"
    _seed(p, [
        ("slab_marble_carrara_0001", "Carrara", "Marble"),
        ("block_marble_carrara_0002", "Carrara", "Marble"),      # same variety, other branch -> ONE target
        ("slab_granite_nero_0003", "Nero Assoluto", "Granite"),
        ("slab_quartzite_retired_0004", "Old Retired", "Quartzite"),
    ])
    monkeypatch.setattr(writethrough, "ledger_path", lambda: p)
    monkeypatch.setattr("stone_pipeline.stages.decisions.load_retired",
                        lambda: {"slab_quartzite_retired_0004"})
    return p


def test_list_all_reads_the_ledger_deduped_and_non_retired(ledger_with_varieties):
    got = varieties.list_all()
    assert [v["name"] for v in got] == ["Carrara", "Nero Assoluto"]   # deduped, retired excluded, sorted
    assert {v["name"]: v["stone_type"] for v in got} == {"Carrara": "Marble", "Nero Assoluto": "Granite"}


def test_exists_uses_the_same_ledger_source(ledger_with_varieties):
    assert varieties.exists("carrara") is True            # normalized match
    assert varieties.exists("Nero Assoluto") is True
    assert varieties.exists("Old Retired") is False       # retired -> not a valid alias target
    assert varieties.exists("Totally Unknown") is False
    assert varieties.exists("") is False


def test_q_and_limit_narrow_the_result(ledger_with_varieties):
    assert [v["name"] for v in varieties.list_all(q="nero")] == ["Nero Assoluto"]
    assert varieties.list_all(q="zzz") == []
    assert len(varieties.list_all(limit=1)) == 1
    # a % in the search term is a literal, not a wildcard
    assert varieties.list_all(q="100%") == []


def test_empty_when_no_ledger_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(writethrough, "ledger_path", lambda: tmp_path / "none.ledger")
    assert varieties.list_all() == []                     # no produce yet -> empty, alias refused (explicit)
    assert varieties.exists("Carrara") is False


def test_dispatch_plumbs_query_params_to_varieties(ledger_with_varieties):
    from stone_pipeline.config import server
    code, body = server.dispatch("GET", ["varieties"], None, "q=nero&limit=5")
    assert code == 200 and [v["name"] for v in body["varieties"]] == ["Nero Assoluto"]
    # no query -> the full (deduped, non-retired) set
    assert len(server.dispatch("GET", ["varieties"], None)[1]["varieties"]) == 2


def test_multi_type_name_keeps_both_types_as_distinct_alias_targets(tmp_path, monkeypatch):
    # A legitimately multi-type name ('Coffee' = Marble AND Onyx) must appear as TWO alias targets, not one
    # -- else the alias dropdown hides one stone (dedup on (name,type), not name). Same (name,type) across
    # branches still collapses to one.
    p = tmp_path / "dev.ledger"
    _seed(p, [
        ("slab_marble_coffee_0001", "Coffee", "Marble"),
        ("tile_marble_coffee_0002", "Coffee", "Marble"),   # same name+type, other branch -> ONE target
        ("slab_onyx_coffee_0003", "Coffee", "Onyx"),        # same name, DIFFERENT type -> a SECOND target
    ])
    monkeypatch.setattr(writethrough, "ledger_path", lambda: p)
    monkeypatch.setattr("stone_pipeline.stages.decisions.load_retired", lambda: set())
    pairs = sorted((v["name"], v["stone_type"]) for v in varieties.list_all(q="coffee"))
    assert pairs == [("Coffee", "Marble"), ("Coffee", "Onyx")]
