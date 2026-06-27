"""Tests for the crash-safe + spreadsheet-safe CSV writer (csvio).

Locks in the two guarantees the rest of the pipeline relies on: formula-injection sanitization for
operator-opened files, and atomic writes (a crash mid-write never leaves a partial/truncated file).
"""

from __future__ import annotations

import csv

import pytest

from stone_pipeline.core import csvio


# --- safe_cell / sanitize ----------------------------------------------------
@pytest.mark.parametrize("leader", ["=", "+", "-", "@", "\t", "\r"])
def test_safe_cell_neutralizes_formula_leaders(leader):
    assert csvio.safe_cell(f"{leader}HYPERLINK(1)") == f"'{leader}HYPERLINK(1)"

def test_safe_cell_passes_benign_values():
    assert csvio.safe_cell("Carrara") == "Carrara"
    assert csvio.safe_cell("A4") == "A4"
    assert csvio.safe_cell("Bianco 2cm") == "Bianco 2cm"

def test_safe_cell_none_is_empty():
    assert csvio.safe_cell(None) == ""

def test_write_dicts_sanitize_roundtrip(tmp_path):
    p = tmp_path / "review.csv"
    csvio.write_dicts(p, ["name"], [{"name": '=cmd|"/c calc"'}], sanitize=True)
    with p.open(encoding="utf-8-sig", newline="") as h:
        row = next(csv.DictReader(h))
    assert row["name"].startswith("'=")          # neutralized on disk

def test_write_dicts_no_sanitize_preserves_value(tmp_path):
    # the Medusa-import path must NOT prepend a quote (it would corrupt the stored name).
    p = tmp_path / "upload.csv"
    csvio.write_dicts(p, ["name"], [{"name": "-Bianco"}], sanitize=False)
    with p.open(encoding="utf-8-sig", newline="") as h:
        row = next(csv.DictReader(h))
    assert row["name"] == "-Bianco"


# --- atomic_write ------------------------------------------------------------
def test_atomic_write_happy_path(tmp_path):
    p = tmp_path / "out.txt"
    csvio.atomic_write(p, lambda h: h.write("done"))
    assert p.read_text() == "done"
    assert not (tmp_path / "out.txt.tmp").exists()    # temp cleaned up

def test_atomic_write_leaves_prior_file_intact_on_failure(tmp_path):
    p = tmp_path / "out.txt"
    p.write_text("ORIGINAL")
    def boom(h):
        h.write("partial")
        raise RuntimeError("crash mid-write")
    with pytest.raises(RuntimeError):
        csvio.atomic_write(p, boom)
    assert p.read_text() == "ORIGINAL"                # never a partial overwrite
    assert not (tmp_path / "out.txt.tmp").exists()    # temp cleaned up in finally

def test_atomic_write_new_file_absent_on_failure(tmp_path):
    p = tmp_path / "new.txt"
    def boom(h):
        h.write("partial")
        raise RuntimeError("crash")
    with pytest.raises(RuntimeError):
        csvio.atomic_write(p, boom)
    assert not p.exists()                              # a failed first write leaves nothing
    assert not (tmp_path / "new.txt.tmp").exists()
