"""GET /sync/v1/status reports every state a table can hold, 0 when it holds none: a count is always a value, never
an absent key (the admin's sync page rendered an absent state as 'None'). The states are declared once beside the
schema; this pins them to schema.sql's own CHECK constraints so the two can never drift."""

from __future__ import annotations

import re
from pathlib import Path

from stone_pipeline.ledger import db
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import status

_SCHEMA = Path(db.__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _check_states(table: str) -> tuple[str, ...]:
    body = re.search(rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", _SCHEMA, re.DOTALL).group(1)
    allowed = re.search(r"CHECK\s*\(\s*state\s+IN\s*\(([^)]*)\)", body).group(1)
    return tuple(re.findall(r"'([a-z_]+)'", allowed))


def test_the_declared_states_are_the_schema_s_own():
    for table, states in db.STATES.items():
        assert states == _check_states(table), table


def test_an_empty_ledger_reports_every_state_as_zero(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        out = status(ledger)
    for table, states in db.STATES.items():
        assert out[table] == dict.fromkeys(states, 0), table


def test_a_held_state_keeps_its_count_beside_the_zeros(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        ledger.upsert("attribute", {"category": "color", "value": "Black", "medusa_id": "C1", "state": "synced",
                                    "created_at": now, "updated_at": now}, pk=("category", "value"))
        out = status(ledger)
    assert out["attribute"] == {"pending": 0, "dirty": 0, "syncing": 0, "synced": 1, "needs_resync": 0}
