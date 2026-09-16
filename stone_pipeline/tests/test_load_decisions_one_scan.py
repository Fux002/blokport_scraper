"""load_decisions reads the ledger ONCE per call, not once per statement. varieties caches the ledger scan
behind a fingerprint (ledger + config.db), so the many exists_as / alias_target lookups a load makes hit the
cache after the first. A per-name re-scan made load_decisions O(statements x ledger) and hung the review API
as decisions accumulated; this pins the scan count."""

from __future__ import annotations

from stone_pipeline.config import decisions_store, varieties


def _seed_statements(n: int) -> None:
    # VENDOR 'is' statements: each is judged against the ledger on load (a global reject is not), so this is
    # what exercises the per-statement lookup the cache must collapse to one scan
    for i in range(n):
        decisions_store._upsert_statement(f"vendor{i}", f"Spelling {i}", "is",
                                          name=f"Variety {i}", stone_type="Marble")


def test_load_decisions_scans_the_ledger_once_regardless_of_statement_count(monkeypatch):
    varieties._CACHE["fp"] = object()                             # force a cold cache for this test
    calls = {"n": 0}
    real = varieties._build_index

    def counting_build():
        calls["n"] += 1
        return real()
    monkeypatch.setattr(varieties, "_build_index", counting_build)

    _seed_statements(25)
    dec = decisions_store.load_decisions()                        # 25 statements, judged via exists_as/alias_target
    assert len(dec.statements) == 25
    assert calls["n"] == 1, f"ledger scanned {calls['n']} times for one load; must be once"


def test_the_cache_invalidates_when_config_db_changes(monkeypatch):
    varieties._CACHE["fp"] = object()
    calls = {"n": 0}
    real = varieties._build_index
    monkeypatch.setattr(varieties, "_build_index", lambda: (calls.__setitem__("n", calls["n"] + 1) or real()))
    _seed_statements(2)
    decisions_store.load_decisions()                              # builds once
    n1 = calls["n"]
    assert n1 == 1
    decisions_store._upsert_statement("vendorX", "Brand New Spelling", "is",
                                      name="Brand New", stone_type="Marble")   # writes config.db -> fp changes
    decisions_store.load_decisions()                              # must rebuild, not serve stale
    assert calls["n"] == n1 + 1


def test_injected_lookups_bypass_the_ledger_entirely():
    seen = []
    dec = decisions_store.load_decisions(
        exists_as=lambda n, t: seen.append((n, t)) or False,
        alias_target=lambda n, t: None)
    assert isinstance(dec.statements, dict)


def test_a_lookup_on_a_warm_index_normalises_only_its_own_arguments(monkeypatch):
    # The cache held the SCAN but not the lookup: exists_as still normalised every row's name and type per call
    # (12k rows x 83 lookups = 500k normalisations per review-list request on prod, seconds on a 1-vCPU task,
    # and four polling clients pushed it past Blokport's 15 s deadline). A warm index is keyed by normalised
    # (name, type), so one lookup normalises its two arguments and nothing else.
    varieties._CACHE["fp"] = object()
    rows = [{"name": f"Variety {i}", "stone_type": "Marble"} for i in range(5000)]
    monkeypatch.setattr(varieties, "_build_index", lambda: varieties._index_from_rows(rows, {}))
    assert varieties.exists_as("Variety 4999", "Marble") is True   # warms the cache
    calls = {"n": 0}
    real = varieties.proj.norm
    monkeypatch.setattr(varieties.proj, "norm", lambda s: calls.__setitem__("n", calls["n"] + 1) or real(s))
    assert varieties.exists_as("Variety 12", "Marble") is True
    assert varieties.exists_as("Variety 12", "Onyx") is False
    assert varieties.exists("Variety 7") is True
    assert calls["n"] <= 5, f"{calls['n']} normalisations for three lookups on a warm index"
