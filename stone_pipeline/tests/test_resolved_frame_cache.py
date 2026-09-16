"""The resolved list re-read every canonical parquet on every call (about a second cold for four sources on
prod, several on the 1-vCPU task, and the admin's vendor switch waited for it). The parsed frames are cached
behind the files' identity (path, size, mtime), like the varieties index: an unchanged outputs tree is read
once, a produce that rewrites a file is read again."""

from __future__ import annotations

import os

import polars as pl

from stone_pipeline.config import resolved

COLS = list(resolved._COLUMNS)


def _write_canonical(outputs, source, run, rows):
    d = outputs / f"{source}_{run}" / "diagnostics"
    d.mkdir(parents=True, exist_ok=True)
    pl.DataFrame([{c: r.get(c, "") for c in COLS} for r in rows]).write_parquet(d / "canonical.parquet")
    return d / "canonical.parquet"


def _rec(src, key, name, vk):
    return {"src_site": src, "surrogate_key": key, "raw_name": name, "variety_match_key": name,
            "variation_key": vk, "variation_name": name, "type_name": "Marble"}


def test_an_unchanged_outputs_tree_is_read_once(tmp_path, monkeypatch):
    f = _write_canonical(tmp_path, "zucchi", "20260916_000000", [_rec("zucchi", "1", "Alpine", "k1")])
    reads = {"n": 0}
    real = pl.read_parquet
    monkeypatch.setattr(pl, "read_parquet", lambda *a, **k: reads.__setitem__("n", reads["n"] + 1) or real(*a, **k))
    assert len(resolved.list_resolved(outputs_dir=tmp_path)) == 1
    assert len(resolved.list_resolved(source="zucchi", outputs_dir=tmp_path)) == 1
    assert reads["n"] == 1, f"parquet read {reads['n']} times for an unchanged tree"

    # a produce rewrote the file (new bytes, new mtime): it must be read again and the new row served
    _write_canonical(tmp_path, "zucchi", "20260916_000000", [_rec("zucchi", "1", "Alpine", "k1"), _rec("zucchi", "2", "Bosco", "k2")])
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    assert len(resolved.list_resolved(outputs_dir=tmp_path)) == 2
    assert reads["n"] == 2
