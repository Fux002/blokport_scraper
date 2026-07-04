"""produce.py orchestration: the full from-scratch produce = fetch export inputs -> LIVE scrape ->
build. This is what the `/run` trigger runs so a produce on a fresh host (empty data/) populates from
nothing. The order matters (scrape before build), catalog-only must NOT scrape (it re-consolidates
existing outputs), a failed scrape must abort before build, and fetch_inputs is best-effort (a fetch
failure never blocks the produce)."""

from __future__ import annotations

from stone_pipeline import produce


def _stub(monkeypatch):
    """Record the produce steps in order; stub build.main so nothing touches disk/network."""
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append(("fetch", None)))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append(("scrape", s)) or 0)
    monkeypatch.setattr(produce.build, "main", lambda argv: calls.append(("build", argv)) or 0)
    return calls


def test_full_produce_fetches_then_scrapes_then_builds(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--sources", "zucchi"]) == 0
    assert calls == [("fetch", None), ("scrape", ["zucchi"]), ("build", ["--sources", "zucchi"])]


def test_default_scope_scrapes_all(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main([]) == 0
    assert calls == [("fetch", None), ("scrape", None), ("build", [])]


def test_catalog_only_never_scrapes(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--stage", "catalog"]) == 0
    assert calls == [("fetch", None), ("build", ["--stage", "catalog"])]   # fetch, then straight to build


def test_inventory_stage_still_scrapes_first(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--stage", "inventory"]) == 0
    assert calls == [("fetch", None), ("scrape", None), ("build", ["--stage", "inventory"])]


def test_verify_delegates_straight_to_build(monkeypatch):
    calls = _stub(monkeypatch)
    assert produce.main(["--verify"]) == 0
    assert calls == [("build", ["--verify"])]        # no fetch, no scrape


def test_failed_scrape_aborts_before_build(monkeypatch):
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append("fetch"))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append("scrape") or 3)
    monkeypatch.setattr(produce.build, "main", lambda argv: calls.append("build") or 0)
    assert produce.main(["--sources", "zucchi"]) == 3
    assert calls == ["fetch", "scrape"]              # build never runs on a failed scrape


def test_fetch_inputs_failure_is_best_effort(monkeypatch):
    """A fetch failure logs and continues -- it must not raise out of the produce."""
    def boom():
        raise RuntimeError("no S3 here")
    import stone_pipeline.produce as p
    monkeypatch.setattr("deploy.fetch_inputs.main", boom, raising=False)
    # call the real _fetch_inputs (not the stub) to prove it swallows the error
    p._fetch_inputs()   # must not raise
