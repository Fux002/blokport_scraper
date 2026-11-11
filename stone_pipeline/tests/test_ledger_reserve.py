"""POST /sync/v1/reserve (P2-10, 173 F18): the backend's variety restore asks the ledger to re-serve exactly the
restored Keys once, so each comes back with its whole payload. The answer is {reserved, unknown, frozen}, whose
external_ids are disjoint and together exactly the deduped external_ids sent. `reserved` WILL serve on a coming
pull (a retry wait only delays it); an unknown Key is reported, never created; a frozen Key exists but the scraper
does not serve it, so it is never marked: {external_id, reason}, reason the first of abandoned | archived |
retiring | not_in_full | no_type. A reserve outranks every lease issued before it (the reserved_at stamp). At most
SETTINGS.sync.max_reserve_ids distinct Keys per call; a call over the bound is refused whole with nothing marked.
"""

from __future__ import annotations

import io
import json
import sqlite3

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger
from stone_pipeline.ledger.server import SyncHandler, dispatch
from stone_pipeline.ledger.sync import ack, ready

_OLD = "2000-01-01T00:00:00+00:00"


def _variation(ledger, key, state="synced", *, archived=0, in_full=1, type_="Marble", payload_hash="h"):
    ledger.upsert("variation", {
        "key": key, "type": type_, "name": "x", "aliases": "[]",
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
        _variation(ledger, "marble__a_1")
        _variation(ledger, "marble__b_2")                      # not sent: never marked
        assert ready(ledger, "variations") == []
        assert _reserve(ledger, ["marble__a_1"]) == (200, {"reserved": ["marble__a_1"], "unknown": [], "frozen": []})
        assert _state(ledger, "marble__b_2") == "synced"
        served = ready(ledger, "variations")
        assert [i["external_id"] for i in served] == ["marble__a_1"]
        ack(ledger, "variations", "marble__a_1", "V", payload_hash=served[0]["payload_hash"])
        assert _state(ledger, "marble__a_1") == "synced"
        assert ready(ledger, "variations") == []            # once, not on every pull


def test_unknown_is_reported_and_never_created(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        assert _reserve(ledger, ["marble__zz_9", "marble__a_1"]) == (
            200, {"reserved": ["marble__a_1"], "unknown": ["marble__zz_9"], "frozen": []})
        assert ledger.get("variation", "key", "marble__zz_9") is None


def _abandon(ledger, key):
    """The operator's terminal verdict on a dead-letter (sync.abandon_dead_letter's write)."""
    ledger.execute("UPDATE variation SET state = 'gap_held', abandoned_at = ? WHERE key = ?", (_OLD, key))


def _frozen_alone(tmp_path, abandoned=False, **row) -> list[dict]:
    """Reserve one row built with `row`; assert it is frozen, nothing else, and wholly untouched. Returns frozen."""
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1", **row)
        if abandoned:
            _abandon(ledger, "marble__a_1")
        before = dict(ledger.get("variation", "key", "marble__a_1"))
        code, answer = _reserve(ledger, ["marble__a_1"])
        assert code == 200 and answer["reserved"] == [] and answer["unknown"] == []
        assert dict(ledger.get("variation", "key", "marble__a_1")) == before     # never marked, never stamped
        return answer["frozen"]


def test_an_abandoned_row_is_frozen_abandoned_and_never_revived(tmp_path):
    # an operator-abandoned dead-letter is terminal: a reserve that revived it would, on a further failure, strand it
    # (requeue skips an abandoned row and abandon answers "already"), so the operator's verdict is respected
    assert _frozen_alone(tmp_path, abandoned=True) == [{"external_id": "marble__a_1", "reason": "abandoned"}]


def test_an_archived_row_is_frozen_archived(tmp_path):
    assert _frozen_alone(tmp_path, archived=1) == [{"external_id": "marble__a_1", "reason": "archived"}]


def test_a_retiring_row_is_frozen_retiring(tmp_path):
    assert _frozen_alone(tmp_path, state="retiring") == [{"external_id": "marble__a_1", "reason": "retiring"}]


def test_a_row_outside_the_produced_set_is_frozen_not_in_full(tmp_path):
    assert _frozen_alone(tmp_path, in_full=0) == [{"external_id": "marble__a_1", "reason": "not_in_full"}]


def test_an_untyped_row_is_frozen_no_type(tmp_path):
    assert _frozen_alone(tmp_path, type_="") == [{"external_id": "marble__a_1", "reason": "no_type"}]


def test_the_frozen_reason_is_the_first_that_applies(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1", "retiring", archived=1, in_full=0, type_="")
        _variation(ledger, "marble__b_2", "retiring", in_full=0, type_="")
        _variation(ledger, "marble__c_3", in_full=0, type_="")
        _variation(ledger, "marble__d_4", "syncing", archived=1)            # archived beats a live lease too
        _variation(ledger, "marble__e_5", archived=1, in_full=0)
        _abandon(ledger, "marble__e_5")                                      # the operator's ruling outranks the rest
        assert _reserve(ledger, ["marble__e_5", "marble__d_4", "marble__c_3", "marble__b_2", "marble__a_1"])[1]["frozen"] == [
            {"external_id": "marble__a_1", "reason": "archived"}, {"external_id": "marble__b_2", "reason": "retiring"},
            {"external_id": "marble__c_3", "reason": "not_in_full"}, {"external_id": "marble__d_4", "reason": "archived"},
            {"external_id": "marble__e_5", "reason": "abandoned"}]
        assert _state(ledger, "marble__d_4") == "syncing"


def test_a_row_waiting_out_a_failed_apply_is_reserved_and_serves_after_the_wait(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        ledger.execute("UPDATE variation SET retry_after = '2999-01-01T00:00:00+00:00'")
        assert _reserve(ledger, ["marble__a_1"])[1]["reserved"] == ["marble__a_1"]
        assert ready(ledger, "variations") == []                         # only delayed
        ledger.execute("UPDATE variation SET retry_after = ?", (_OLD,))
        assert [i["external_id"] for i in ready(ledger, "variations")] == ["marble__a_1"]


def test_every_reserved_row_is_stamped_and_each_held_state_is_marked_dirty(tmp_path):
    # synced, needs_resync and a dead-letter are marked dirty (each re-serves once); a mid-lease row follows the
    # produce's content-change rule (syncing -> dirty, populate.py). pending and dirty already serve once and keep
    # their state. Every reserved row is stamped reserved_at, so no lease issued before the reserve can ack it away
    # (a dirty row may still have a reaped lease out).
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        for key, state in [("marble__a_1", "synced"), ("marble__b_2", "needs_resync"), ("marble__c_3", "gap_held"),
                           ("marble__d_4", "syncing"), ("marble__e_5", "pending"), ("marble__f_6", "dirty")]:
            _variation(ledger, key, state)
        keys = ["marble__a_1", "marble__b_2", "marble__c_3", "marble__d_4", "marble__e_5", "marble__f_6"]
        assert _reserve(ledger, keys) == (200, {"reserved": keys, "unknown": [], "frozen": []})
        rows = {r["key"]: r for r in ledger.execute("SELECT key, state, reserved_at FROM variation")}
        assert {k: r["state"] for k, r in rows.items()} == {
            "marble__a_1": "dirty", "marble__b_2": "dirty", "marble__c_3": "dirty", "marble__d_4": "dirty",
            "marble__e_5": "pending", "marble__f_6": "dirty"}
        assert all(r["reserved_at"] for r in rows.values())


def test_reserve_outranks_a_lease_issued_before_it(tmp_path):
    # 34's review of #591: the ack does not require 'syncing', so without a stamp an in-flight ack of a lease issued
    # BEFORE the reserve, echoing the same hash, would set the just-reserved row back to synced, silently. The
    # reserve outranks it: that ack records what Medusa did but leaves the row dirty, and the next pull serves it.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1", "dirty")
        leased = ready(ledger, "variations")                         # the lease, issued before the reserve
        assert [i["external_id"] for i in leased] == ["marble__a_1"] and _state(ledger, "marble__a_1") == "syncing"
        assert _reserve(ledger, ["marble__a_1"])[1]["reserved"] == ["marble__a_1"]
        assert ack(ledger, "variations", "marble__a_1", "V-2", payload_hash=leased[0]["payload_hash"]) == 1
        row = ledger.get("variation", "key", "marble__a_1")
        assert row["state"] == "dirty" and row["medusa_id"] == "V-2" and row["last_synced"]   # recorded, not synced
        served = ready(ledger, "variations")                         # the next pull serves it
        assert [i["external_id"] for i in served] == ["marble__a_1"]
        assert ack(ledger, "variations", "marble__a_1", "V-2", payload_hash=served[0]["payload_hash"]) == 1
        assert _state(ledger, "marble__a_1") == "synced"
        assert ready(ledger, "variations") == []                     # once


def test_a_lease_issued_after_a_reserve_acks_to_synced(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        _reserve(ledger, ["marble__a_1"])
        leased = ready(ledger, "variations")                         # carries the reserved payload
        assert [i["external_id"] for i in leased] == ["marble__a_1"]
        assert ledger.get("variation", "key", "marble__a_1")["reserved_at"] is None   # the lease cleared the stamp
        ack(ledger, "variations", "marble__a_1", "V", payload_hash=leased[0]["payload_hash"])
        assert _state(ledger, "marble__a_1") == "synced"
        assert ready(ledger, "variations") == []


def test_a_second_reserve_before_the_next_pull_changes_nothing_more(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        _variation(ledger, "marble__b_2", "pending")
        first = _reserve(ledger, ["marble__a_1", "marble__b_2"])
        after_first = [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")]
        assert _reserve(ledger, ["marble__a_1", "marble__b_2"]) == first
        assert [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")] == after_first
        assert [i["external_id"] for i in ready(ledger, "variations")] == ["marble__a_1", "marble__b_2"]
        assert ready(ledger, "variations") == []


def test_a_v14_ledger_gains_reserved_at_and_keeps_its_data(tmp_path):
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _variation(lg, "marble__a_1")
    raw = sqlite3.connect(p)                                         # simulate a v14 ledger
    raw.execute("ALTER TABLE variation DROP COLUMN reserved_at")
    raw.execute("PRAGMA user_version = 14")
    raw.execute("UPDATE ledger_meta SET schema_version = 14")
    raw.commit()
    raw.close()
    for _ in range(2):                                               # the second open is a no-op
        with Ledger.open(p, env="development") as lg:
            assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 15
            row = lg.get("variation", "key", "marble__a_1")
            assert row["reserved_at"] is None and row["state"] == "synced"


def test_reserve_is_idempotent(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        _variation(ledger, "marble__arch_2", archived=1)
        sent = ["marble__a_1", "marble__arch_2", "marble__zz_9"]
        first = _reserve(ledger, sent)
        after_first = [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")]
        assert _reserve(ledger, sent) == first
        assert [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")] == after_first


def test_the_lists_are_disjoint_and_exactly_the_deduped_ids_sent(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        _variation(ledger, "marble__b_2", "pending")
        _variation(ledger, "marble__arch_3", archived=1)
        _variation(ledger, "marble__ret_4", "retiring")
        _variation(ledger, "marble__out_5", in_full=0)
        _variation(ledger, "marble__untyped_6", type_="")
        _variation(ledger, "marble__dead_7")
        _abandon(ledger, "marble__dead_7")
        sent = ["marble__zz_9", "marble__a_1", "marble__arch_3", "marble__a_1", "marble__ret_4", "marble__b_2", "marble__out_5",
                "marble__untyped_6", "marble__dead_7", "marble__zz_9"]
        code, answer = _reserve(ledger, sent)
        assert code == 200 and set(answer) == {"reserved", "unknown", "frozen"}
        assert all(set(f) == {"external_id", "reason"} for f in answer["frozen"])
        assert {f["reason"] for f in answer["frozen"]} == {"abandoned", "archived", "retiring", "not_in_full",
                                                         "no_type"}
        lists = [answer["reserved"], answer["unknown"], [f["external_id"] for f in answer["frozen"]]]
        flat = [k for lst in lists for k in lst]
        assert len(flat) == len(set(flat))                       # disjoint, and no Key twice within a list
        assert set(flat) == set(sent)                            # together exactly the deduped ids
        assert all(lst == sorted(lst) for lst in lists)          # deterministic order


def test_1000_distinct_ids_are_accepted_and_duplicates_count_once(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        ids = ["marble__a_1"] + [f"marble__zz_{i:04d}" for i in range(999)]
        code, answer = _reserve(ledger, ids + ["marble__a_1"])     # 1,001 entries, 1,000 distinct
        assert code == 200 and answer["reserved"] == ["marble__a_1"] and len(answer["unknown"]) == 999
        assert _state(ledger, "marble__a_1") == "dirty"


def test_1001_distinct_ids_are_refused_whole_and_nothing_is_marked(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        ids = ["marble__a_1"] + [f"marble__zz_{i:04d}" for i in range(1000)]
        code, answer = _reserve(ledger, ids)
        assert code == 400 and "1000" in answer["error"]
        assert _state(ledger, "marble__a_1") == "synced"


def test_a_malformed_body_or_another_type_is_a_400_and_nothing_is_marked(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "marble__a_1")
        for body in [None, [], {"external_ids": ["marble__a_1"]}, {"type": "products", "external_ids": ["marble__a_1"]},
                     {"type": "variations"}, {"type": "variations", "external_ids": "marble__a_1"},
                     {"type": "variations", "external_ids": ["marble__a_1", 7]}]:
            code, answer = dispatch(ledger, "POST", "reserve", {}, body)
            assert code == 400 and answer["error"], body
        assert _state(ledger, "marble__a_1") == "synced"


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
        _variation(ledger, "marble__a_1")
    body = {"type": "variations", "external_ids": ["marble__a_1"]}
    h, out = _handler(tmp_path, "Bearer nope", body)
    h._handle("POST")
    assert out == [(401, {"error": "unauthorized"})]
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        assert _state(ledger, "marble__a_1") == "synced"
    h, out = _handler(tmp_path, "Bearer sekret", body)
    h._handle("POST")
    assert out == [(200, {"reserved": ["marble__a_1"], "unknown": [], "frozen": []})]
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        assert _state(ledger, "marble__a_1") == "dirty"
