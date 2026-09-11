"""lifecycle verbs that touch config.db do so INSIDE the exclusive slot, never after it is released: a produce
starting in that window would otherwise read the old retire memory / source list and undo the verb."""

from __future__ import annotations

import pytest

from stone_pipeline import lifecycle
from stone_pipeline.stages import decisions


def _slot_recorder(calls):
    def _fake_ledger_op(name, work):
        class _Sync:
            def retire_variation(self, lg, key, force=False): return {"retired": key}
            def un_retire_variation(self, lg, key): return {"un_retired": key}
            def variation_live_products(self, lg, key): return 0
            def variation_exists(self, lg, key): return True
        class _Ledger:
            def get(self, table, col, key): return {"key": key}
        calls.append("slot_enter")
        try:
            out = work(_Ledger(), _Sync())
        finally:
            calls.append("slot_release")
        return out, 200
    return _fake_ledger_op


@pytest.mark.parametrize("verb,memory", [
    (lambda: lifecycle.retire_variation("slab_granite_x_1"), "add_retired"),
    (lambda: lifecycle.un_retire("slab_granite_x_1"), "remove_retired"),
])
def test_retire_memory_is_written_inside_the_slot(monkeypatch, verb, memory):
    calls: list[str] = []
    monkeypatch.setattr(lifecycle, "_ledger_op", _slot_recorder(calls))
    monkeypatch.setattr(lifecycle, "_snapshot_config", lambda: None, raising=False)
    monkeypatch.setattr(decisions, "add_retired", lambda key: calls.append("add_retired"))
    monkeypatch.setattr(decisions, "remove_retired", lambda key: calls.append("remove_retired"))
    for name in ("_variation_exists", "_require_variation"):
        if hasattr(lifecycle, name):
            monkeypatch.setattr(lifecycle, name, lambda *a, **k: True)
    result, code = verb()
    assert code == 200, result
    assert calls.index(memory) < calls.index("slot_release"), calls
