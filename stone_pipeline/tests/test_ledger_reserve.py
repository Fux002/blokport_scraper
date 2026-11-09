"""POST /sync/v1/reserve (P2-10, 173 F18): the backend's variety restore asks the ledger to re-serve exactly the
restored Keys once, so each comes back with its whole payload. The answer is {reserved, unknown, frozen}: three
disjoint lists that together are exactly the deduped external_ids sent. An unknown Key is reported, never created;
a frozen Key (archived, or retiring) exists but is never marked. At most SETTINGS.sync.max_reserve_ids distinct
Keys per call; a call over the bound is refused whole with nothing marked.
"""

from __future__ import annotations

import io
import json

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.server import SyncHandler, dispatch
from stone_pipeline.ledger.sync import ack, ready

_OLD = "2000-01-01T00:00:00+00:00"


def _variation(ledger, key, state="synced", *, archived=0, in_full=1, type_="Marble", payload_hash="h"):
    ledger.upsert("variation", {
        "key": key, "branch": "slab", "type": type_, "name": "x", "aliases": "[]",
        "image_url": "https://s3/tex.png", "image_sha256": None, "image_model": None, "volume": "",
        "medusa_id": "V" if state != "pending" else None, "in_full": in_full, "payload_hash": payload_hash,
        "state": state, "archived": archived, "first_seen": _OLD, "last_synced": None,
        "created_at": _OLD, "updated_at": _OLD}, pk=("key",))


def _reserve(ledger, external_ids, type_="variations"):
    return dispatch(ledger, "POST", "reserve", {}, {"type": type_, "external_ids": external_ids})


def _state(ledger, key):
    return ledger.get("variation", "key", key)["state"]


def test_the_bound_is_a_contract_value_beside_the_body_cap():
    assert SETTINGS.sync.max_reserve_ids == 1000          # BlokPort's RESERVE_PAGE: a change is a paired PR


def test_a_reserved_synced_row_is_served_exactly_once(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
        _variation(ledger, "slab_b_2")                      # not sent: never marked
        assert ready(ledger, "variations") == []
        assert _reserve(ledger, ["slab_a_1"]) == (200, {"reserved": ["slab_a_1"], "unknown": [], "frozen": []})
        assert _state(ledger, "slab_b_2") == "synced"
        served = ready(ledger, "variations")
        assert [i["external_id"] for i in served] == ["slab_a_1"]
        ack(ledger, "variations", "slab_a_1", "V", payload_hash=served[0]["payload_hash"])
        assert _state(ledger, "slab_a_1") == "synced"
        assert ready(ledger, "variations") == []            # once, not on every pull


def test_unknown_is_reported_and_never_created(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
        assert _reserve(ledger, ["slab_zz_9", "slab_a_1"]) == (
            200, {"reserved": ["slab_a_1"], "unknown": ["slab_zz_9"], "frozen": []})
        assert ledger.get("variation", "key", "slab_zz_9") is None


def test_archived_and_retiring_rows_are_frozen_and_never_marked(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_arch_1", archived=1)
        _variation(ledger, "slab_arch_dirty_2", "syncing", archived=1)
        _variation(ledger, "slab_ret_3", "retiring")
        before = {r["key"]: dict(r) for r in ledger.execute("SELECT * FROM variation")}
        assert _reserve(ledger, ["slab_ret_3", "slab_arch_1", "slab_arch_dirty_2"]) == (
            200, {"reserved": [], "unknown": [], "frozen": ["slab_arch_1", "slab_arch_dirty_2", "slab_ret_3"]})
        assert {r["key"]: dict(r) for r in ledger.execute("SELECT * FROM variation")} == before


def test_every_held_state_is_marked_and_pending_or_dirty_rows_are_left_as_they_are(tmp_path):
    # synced, needs_resync and a dead-letter are marked dirty (each re-serves once); a mid-lease row follows the
    # produce's content-change rule (syncing -> dirty, populate.py). pending and dirty already serve once: untouched.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        for key, state in [("slab_a_1", "synced"), ("slab_b_2", "needs_resync"), ("slab_c_3", "gap_held"),
                           ("slab_d_4", "syncing"), ("slab_e_5", "pending"), ("slab_f_6", "dirty")]:
            _variation(ledger, key, state)
        keys = ["slab_a_1", "slab_b_2", "slab_c_3", "slab_d_4", "slab_e_5", "slab_f_6"]
        assert _reserve(ledger, keys) == (200, {"reserved": keys, "unknown": [], "frozen": []})
        rows = {r["key"]: r for r in ledger.execute("SELECT key, state, updated_at FROM variation")}
        assert {k: r["state"] for k, r in rows.items()} == {
            "slab_a_1": "dirty", "slab_b_2": "dirty", "slab_c_3": "dirty", "slab_d_4": "dirty",
            "slab_e_5": "pending", "slab_f_6": "dirty"}
        assert {k for k, r in rows.items() if r["updated_at"] != _OLD} == {"slab_a_1", "slab_b_2", "slab_c_3",
                                                                          "slab_d_4"}


def test_reserve_is_idempotent(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
        _variation(ledger, "slab_arch_2", archived=1)
        sent = ["slab_a_1", "slab_arch_2", "slab_zz_9"]
        first = _reserve(ledger, sent)
        after_first = [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")]
        assert _reserve(ledger, sent) == first
        assert [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")] == after_first


def test_the_lists_are_disjoint_and_exactly_the_deduped_ids_sent(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
        _variation(ledger, "slab_b_2", "pending")
        _variation(ledger, "slab_arch_3", archived=1)
        _variation(ledger, "slab_ret_4", "retiring")
        sent = ["slab_zz_9", "slab_a_1", "slab_arch_3", "slab_a_1", "slab_ret_4", "slab_b_2", "slab_zz_9"]
        code, answer = _reserve(ledger, sent)
        assert code == 200 and set(answer) == {"reserved", "unknown", "frozen"}
        lists = [answer["reserved"], answer["unknown"], answer["frozen"]]
        flat = [k for lst in lists for k in lst]
        assert len(flat) == len(set(flat))                       # disjoint, and no Key twice within a list
        assert set(flat) == set(sent)                            # together exactly the deduped ids
        assert all(lst == sorted(lst) for lst in lists)          # deterministic order


def test_1000_distinct_ids_are_accepted_and_duplicates_count_once(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
        ids = ["slab_a_1"] + [f"slab_zz_{i:04d}" for i in range(999)]
        code, answer = _reserve(ledger, ids + ["slab_a_1"])     # 1,001 entries, 1,000 distinct
        assert code == 200 and answer["reserved"] == ["slab_a_1"] and len(answer["unknown"]) == 999
        assert _state(ledger, "slab_a_1") == "dirty"


def test_1001_distinct_ids_are_refused_whole_and_nothing_is_marked(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
        ids = ["slab_a_1"] + [f"slab_zz_{i:04d}" for i in range(1000)]
        code, answer = _reserve(ledger, ids)
        assert code == 400 and "1000" in answer["error"]
        assert _state(ledger, "slab_a_1") == "synced"


def test_a_malformed_body_or_another_type_is_a_400_and_nothing_is_marked(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
        for body in [None, [], {"external_ids": ["slab_a_1"]}, {"type": "products", "external_ids": ["slab_a_1"]},
                     {"type": "variations"}, {"type": "variations", "external_ids": "slab_a_1"},
                     {"type": "variations", "external_ids": ["slab_a_1", 7]}]:
            code, answer = dispatch(ledger, "POST", "reserve", {}, body)
            assert code == 400 and answer["error"], body
        assert _state(ledger, "slab_a_1") == "synced"


def _handler(tmp_path, token_sent: str, body: dict) -> tuple[SyncHandler, list]:
    raw = json.dumps(body).encode("utf-8")
    h = SyncHandler.__new__(SyncHandler)               # bypass BaseHTTPRequestHandler.__init__ (opens a socket)
    h.headers = {"Authorization": token_sent, "Content-Length": str(len(raw))}
    h.server = type("S", (), {"expected_token": "sekret", "ledger_path": tmp_path / "dev.ledger"})()
    h.path = "/sync/v1/reserve"
    h.rfile = io.BytesIO(raw)
    out: list = []
    h._respond = lambda code, payload: out.append((code, payload))
    return h, out


def test_reserve_takes_the_same_bearer_auth_as_every_sync_route(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_a_1")
    body = {"type": "variations", "external_ids": ["slab_a_1"]}
    h, out = _handler(tmp_path, "Bearer nope", body)
    h._handle("POST")
    assert out == [(401, {"error": "unauthorized"})]
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        assert _state(ledger, "slab_a_1") == "synced"
    h, out = _handler(tmp_path, "Bearer sekret", body)
    h._handle("POST")
    assert out == [(200, {"reserved": ["slab_a_1"], "unknown": [], "frozen": []})]
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        assert _state(ledger, "slab_a_1") == "dirty"
