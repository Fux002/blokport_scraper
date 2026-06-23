"""The decision ledger read-back: variants_to_confirm.csv true/false/blank, parsed into
yes/no/pending, and the persistent reject memory."""

from __future__ import annotations

import csv

from stone_pipeline.stages import decisions


def _write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=decisions.CONFIRM_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def test_confirm_decisions_parse_true_false_blank(tmp_path):
    p = tmp_path / "variants_to_confirm.csv"
    _write(p, [
        {"confirm": "true", "variant": "Alpha Stone"},
        {"confirm": "yes", "variant": "Beta Stone"},
        {"confirm": "false", "variant": "Gamma Stone"},
        {"confirm": "", "variant": "Delta Stone"},          # pending -> omitted
    ])
    d = decisions.load_confirm_decisions(p)
    assert d == {"alpha stone": "yes", "beta stone": "yes", "gamma stone": "no"}
    assert "delta stone" not in d                            # blank = pending, not a decision


def test_write_confirm_file_keeps_only_pending(tmp_path):
    out = tmp_path / "variants_to_confirm.csv"
    decisions.write_confirm_file(
        [{"confirm": "", "variant": "Still Pending", "stone_type": "Marble"}], out)
    rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
    assert [r["variant"] for r in rows] == ["Still Pending"]
    assert rows[0]["confirm"] == ""                          # rewritten blank, ready for a decision


def test_rejected_round_trip(tmp_path):
    p = tmp_path / "rejected.csv"
    assert decisions.load_rejected(p) == set()
    decisions.save_rejected({"foo", "bar"}, p)
    assert decisions.load_rejected(p) == {"foo", "bar"}      # persists across runs
