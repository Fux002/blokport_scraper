"""An operator write to config.db is snapshotted to S3 as soon as it succeeds, by the same mechanism the lifecycle verbs
use (lifecycle.snapshot_config), so a decision never waits on the periodic save: a task that dies, or is replaced by a
roll, before that save would otherwise take the decision with it. A refused write changes nothing and snapshots nothing.
Every config API write route is covered: either it snapshots through the route table, or it is a lifecycle verb that
snapshots itself."""

from __future__ import annotations

import pytest

from stone_pipeline import lifecycle
from stone_pipeline.config import server
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


# The write routes whose handler is a lifecycle verb that snapshots config.db itself.
_SNAPSHOTS_ITSELF = {
    ("POST", ("reset",)), ("POST", ("delist",)), ("POST", ("pause",)), ("POST", ("resume",)),
    ("DELETE", ("sources", "{name}")),
}
# The write routes that record no operator decision in config.db: they start a run (whose own bookkeeping the boot
# reconciles) or work on the ledger and files.
_NO_OPERATOR_DECISION = {
    ("POST", ("run",)), ("POST", ("curation", "rebuild")), ("POST", ("purge",)), ("POST", ("clean",)),
}


def test_every_config_write_route_snapshots_config_db():
    writes = {(m, p) for m, p, _ in server._ROUTES if m != "GET"}
    durable = {(m, p) for m, p, h in server._ROUTES if getattr(h, "snapshots_config", False)}
    assert writes - _SNAPSHOTS_ITSELF - _NO_OPERATOR_DECISION == durable
