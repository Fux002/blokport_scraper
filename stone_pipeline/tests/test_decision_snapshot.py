"""An operator write to config.db is snapshotted to S3 as soon as it succeeds, by the same mechanism the lifecycle verbs
use (lifecycle.snapshot_config), so a decision never waits on the periodic save: a task that dies, or is replaced by a
roll, before that save would otherwise take the decision with it. A refused write changes nothing and snapshots nothing.
Every config API write route is covered: either it snapshots through the route table, or it is a lifecycle verb that
snapshots itself."""

from __future__ import annotations

import pytest

from stone_pipeline import lifecycle
from stone_pipeline.config.control import server
from stone_pipeline.stages import decisions


@pytest.fixture
def snapshots(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(lifecycle, "snapshot_config", lambda: calls.append("config"))
    return calls


def test_a_decision_written_through_the_api_is_snapshotted_at_once(snapshots):
    decisions.write_attributes_to_add([
        {"kind": "finish", "value": "Leathered", "count": 12, "suggested_value": "", "action": "", "medusa_id": ""}])
    code, _ = server.dispatch("PUT", ["review", "attributes", "Leathered"], {"kind": "finish", "medusa_id": "pcol_9"})
    assert code == 200 and snapshots == ["config"]


def test_an_origin_edit_and_its_undo_are_each_snapshotted(snapshots):
    body = {"variety": "Crystal White", "stone_type": "Granite", "countries": ["IN"]}
    assert server.dispatch("PUT", ["origins", "x"], body)[0] == 200
    assert server.dispatch("DELETE", ["origins", "crystal white|granite"], None)[0] == 200
    assert snapshots == ["config", "config"]


def test_a_refused_write_snapshots_nothing(snapshots):
    assert server.dispatch("PUT", ["review", "attributes", "Honed"], {"kind": "finish"})[0] == 400
    assert server.dispatch("PUT", ["review", "decide"], {"source": "zucchi"})[0] == 400
    assert server.dispatch("DELETE", ["origins", "nothing|here"], None)[0] == 404
    assert snapshots == []


# The write routes whose handler is a lifecycle verb that snapshots config.db itself: proven below, not just listed.
_SNAPSHOTS_ITSELF = {
    ("POST", ("reset",)), ("POST", ("delist",)), ("POST", ("pause",)), ("POST", ("resume",)),
    ("DELETE", ("sources", "{name}")),
}
# The write routes that record no operator decision in config.db: they start a run (whose own bookkeeping the boot
# reconciles) or work on the ledger and files.
_NO_OPERATOR_DECISION = {
    ("POST", ("run",)), ("POST", ("curation", "rebuild")), ("POST", ("purge",)), ("POST", ("clean",)),
    # the owner's click and its withdrawal, and the decision page's export: photo decisions the decisions applier
    # writes to S3 (clicks/, owner/, removed/), durable once written, inside the photo process's run lock; config.db is
    # only read (the sources' image pipeline switch)
    ("PUT", ("photos", "clicks", "{sku}")), ("DELETE", ("photos", "clicks", "{sku}")), ("POST", ("photos", "decisions")),
}
# Every write route that changes the ledger, with a body that reaches its ledger op.
_LEDGER_WRITES = {
    ("POST", ("reset",)): {}, ("POST", ("delist",)): {"sources": ["polonine"]}, ("POST", ("purge",)): {},
    ("DELETE", ("sources", "{name}")): None, ("POST", ("variations", "unmint")): {"keys": ["granite__x_1"]},
    ("POST", ("variations", "{key}", "{verb}")): None,
}


def test_every_config_write_route_snapshots_config_db():
    writes = {(m, p) for m, p, _ in server._ROUTES if m != "GET"}
    durable = {(m, p) for m, p, h in server._ROUTES if getattr(h, "snapshots_config", False)}
    assert writes - _SNAPSHOTS_ITSELF - _NO_OPERATOR_DECISION == durable


@pytest.fixture
def one_source(tmp_path):
    from stone_pipeline.config import store
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    store.seed_from_yaml(yaml_path=yaml_path)


def _canned_ledger_op(name, work):
    """The ledger half answered without a ledger: what each verb reads from a successful op."""
    return {"delisted": 0, "product": 0, "unpublished": 0, "external_ids": [], "config_removed": True,
            "unminted": [], "varieties": [], "skipped": []}, 200


@pytest.mark.parametrize("route,segments,body", [
    (("POST", ("pause",)), ["pause"], {"sources": ["polonine"]}),
    (("POST", ("resume",)), ["resume"], {"sources": ["polonine"]}),
    (("POST", ("delist",)), ["delist"], {"sources": ["polonine"]}),
    (("POST", ("reset",)), ["reset"], {}),
    (("DELETE", ("sources", "{name}")), ["sources", "polonine"], None),
])
def test_the_lifecycle_verbs_listed_as_snapshotting_themselves_do(monkeypatch, snapshots, one_source,
                                                                 route, segments, body):
    assert route in _SNAPSHOTS_ITSELF
    monkeypatch.setattr(lifecycle, "_ledger_op", _canned_ledger_op)
    code, payload = server.dispatch(route[0], segments, body)
    assert code == 200, payload
    assert snapshots == ["config"]


def test_every_self_snapshotting_route_is_proven_above():
    proven = {r for r, _, _ in test_the_lifecycle_verbs_listed_as_snapshotting_themselves_do.pytestmark[0].args[1]}
    assert proven == _SNAPSHOTS_ITSELF


@pytest.mark.parametrize("route", sorted(_LEDGER_WRITES, key=str))
def test_every_ledger_write_route_goes_through_the_one_ledger_op(monkeypatch, one_source, route):
    method, pattern = route
    segments = ["polonine" if t == "{name}" else "granite__x_1" if t == "{key}" else "retire" if t == "{verb}"
                else t for t in pattern]
    reached: list[str] = []
    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: reached.append(name) or _canned_ledger_op(name, work))
    monkeypatch.setattr(lifecycle, "snapshot_config", lambda: None)
    server.dispatch(method, segments, _LEDGER_WRITES[route])
    assert reached, f"{method} /{'/'.join(segments)} changed the ledger outside lifecycle._ledger_op"


def test_the_ledger_op_snapshots_the_ledger_after_a_change_and_not_after_a_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "development.db"))
    saved: list[str] = []
    monkeypatch.setattr(lifecycle, "_snapshot_ledger", lambda: saved.append("ledger"))
    result, code = lifecycle._ledger_op("retire_variation", lambda lg, sync: {"retired": "k"})
    assert (result, code) == ({"retired": "k"}, 200) and saved == ["ledger"]

    def refuse(lg, sync):
        raise lifecycle._Busy("still live")
    assert lifecycle._ledger_op("retire_variation", refuse)[1] == 409
    assert saved == ["ledger"]                                      # a refused op changed nothing
