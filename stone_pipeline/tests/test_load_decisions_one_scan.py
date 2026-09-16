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


def test_concurrent_requests_after_an_invalidation_share_one_rebuild(monkeypatch):
    # prod 2026-09-16: four admin clients poll the review list; when the index invalidates (a ledger or
    # config.db write), every in-flight request rebuilt it in parallel on the 1-vCPU task (a ~1 s laptop build
    # became 5 s each, past Blokport's 15 s deadline). A rebuild is single-flight: the first request builds,
    # the others wait for that build and read it.
    import threading, time
    varieties._CACHE["fp"] = object()
    monkeypatch.setattr(varieties, "_fingerprint", lambda: "same")
    builds = {"n": 0}

    def slow_build():
        builds["n"] += 1
        time.sleep(0.3)
        return varieties._index_from_rows([{"name": "Alpine", "stone_type": "Granite"}], {})
    monkeypatch.setattr(varieties, "_build_index", slow_build)
    results = []
    threads = [threading.Thread(target=lambda: results.append(varieties.exists_as("Alpine", "Granite"))) for _ in range(6)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert results == [True] * 6
    assert builds["n"] == 1, f"{builds['n']} parallel rebuilds for one invalidation"


def test_an_empty_wal_appearing_beside_the_ledger_does_not_invalidate(tmp_path, monkeypatch):
    # SQLite deletes the -wal when the last connection closes and recreates it empty on the next open. Both
    # containers open the ledger per request, so on prod the WAL's existence and mtime flip constantly with
    # no real write, and the index was rebuilt cold on nearly every review request. Only a change of the
    # main file, or WAL frames (a non-zero WAL size), is a change of content.
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.config import store
    ledger = tmp_path / "production.db"; ledger.write_bytes(b"x" * 10)
    cfg = tmp_path / "config.db"; cfg.write_bytes(b"y" * 10)
    monkeypatch.setattr(writethrough, "ledger_path", lambda: ledger)
    monkeypatch.setattr(store, "config_db_path", lambda: cfg)
    varieties._CACHE["fp"] = object()
    builds = {"n": 0}
    monkeypatch.setattr(varieties, "_build_index", lambda: builds.__setitem__("n", builds["n"] + 1) or
                        varieties._index_from_rows([{"name": "Alpine", "stone_type": "Granite"}], {}))
    assert varieties.exists_as("Alpine", "Granite")
    (tmp_path / "production.db-wal").write_bytes(b"")          # a connection opened: empty WAL appears
    (tmp_path / "config.db-wal").write_bytes(b"")
    assert varieties.exists_as("Alpine", "Granite")
    (tmp_path / "production.db-wal").unlink()                  # the last connection closed: WAL gone
    assert varieties.exists_as("Alpine", "Granite")
    assert builds["n"] == 1, f"{builds['n']} rebuilds for WAL files that carried no frames"
    (tmp_path / "production.db-wal").write_bytes(b"frame")     # a real write: frames in the WAL
    assert varieties.exists_as("Alpine", "Granite")
    assert builds["n"] == 2
