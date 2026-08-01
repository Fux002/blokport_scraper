"""F10: the catalog must pick the newest COMPLETE run per source, never an aborted newer one.

An abort (scrape-floor / health / gate) creates the run folder + diagnostics but writes NO
canonical.parquet. If consolidation/prune treated the lexically-newest folder as authoritative, an
aborted newer run would (a) drop the source from the catalog and (b) let prune delete the last good
run in favour of the empty newer folder -- the source silently vanishes and its data is destroyed.
"""

from __future__ import annotations

from stone_pipeline import catalog


def _make_run(root, source, ts, *, complete):
    d = root / f"{source}_{ts}"
    (d / "diagnostics").mkdir(parents=True)
    (d / "diagnostics" / "run.json").write_text("{}")           # every run writes diagnostics
    if complete:
        (d / "diagnostics" / "canonical.parquet").write_bytes(b"PAR1")   # only a successful emit
    return d


def test_latest_run_dirs_picks_newest_complete_not_aborted_newer(tmp_path):
    good = _make_run(tmp_path, "varsha", "20260731_120000", complete=True)
    _make_run(tmp_path, "varsha", "20260801_120000", complete=False)   # aborted, newer
    dirs = catalog.latest_run_dirs(tmp_path)
    assert dirs == [good], "the newest COMPLETE run must win, not the aborted newer folder"
    # and the source is still present in the catalog (its canonical parquet is found)
    assert catalog.find_canonical(tmp_path) == [good / "diagnostics" / "canonical.parquet"]


def test_prune_keeps_the_last_good_run_and_removes_the_aborted_newer(tmp_path):
    good = _make_run(tmp_path, "varsha", "20260731_120000", complete=True)
    aborted = _make_run(tmp_path, "varsha", "20260801_120000", complete=False)
    removed = catalog.prune_superseded_runs(tmp_path)
    assert good.exists(), "last good run must survive prune"
    assert not aborted.exists(), "the empty aborted newer folder is stale -> pruned"
    assert removed == 1


def test_a_newer_complete_run_still_supersedes_the_older_one(tmp_path):
    _make_run(tmp_path, "varsha", "20260731_120000", complete=True)
    newer = _make_run(tmp_path, "varsha", "20260801_120000", complete=True)
    assert catalog.latest_run_dirs(tmp_path) == [newer]     # normal recency still holds when both complete


def test_source_with_only_an_aborted_run_is_absent_not_crashing(tmp_path):
    _make_run(tmp_path, "varsha", "20260801_120000", complete=False)   # first-ever run aborted
    assert catalog.latest_run_dirs(tmp_path) == []
    assert catalog.find_canonical(tmp_path) == []
