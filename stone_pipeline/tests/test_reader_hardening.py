"""Malformed-input reader hardening (audit M1-M4): a truncated/renamed/empty reference or template file must
degrade LOUDLY-but-safely (skip the bad row, or fail with a clear message) the same way reference.loaders
already reads these files -- never a bare KeyError/StopIteration/ValueError that aborts the whole run.
"""

from __future__ import annotations

import pytest


def test_read_template_columns_fails_loud_on_empty_template(tmp_path):
    from stone_pipeline.stages.emit import read_template_columns
    empty = tmp_path / "template.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no header row"):     # not a bare StopIteration
        read_template_columns(empty)


def test_load_units_skips_a_non_numeric_factor_keeps_the_rest(tmp_path):
    from stone_pipeline.reference.loaders import load_units
    csv_path = tmp_path / "units.csv"
    csv_path.write_text("token,dimension,canonical,factor\n"
                        "mm,length,mm,0.1\n"
                        "bad,length,bad,abc\n"          # non-numeric factor -> skipped, not a crash
                        "cm,length,cm,\n", encoding="utf-8")   # empty factor -> legit 1.0
    units = load_units(csv_path)
    assert units.by_token["mm"].factor == 0.1
    assert units.by_token["cm"].factor == 1.0
    assert "bad" not in units.by_token                  # the bad row was skipped, others loaded


def test_load_attributes_tolerates_a_missing_column(tmp_path):
    from stone_pipeline.stages.tree_build import _load_attributes
    csv_path = tmp_path / "attributes.csv"
    # a header missing 'sourceid' + a blank-value row: both are skipped, the good row still loads (no KeyError)
    csv_path.write_text("category,value,sourceid\n"
                        "color,Black,col_1\n"
                        "color,,col_2\n", encoding="utf-8")
    attr = _load_attributes(csv_path)
    assert attr["color"]["black"] == "col_1" and len(attr["color"]) == 1


def test_posts_of_tolerates_both_backbone_shapes():
    from stone_pipeline.stages.emit_catalog import _posts_of
    assert _posts_of({"posts": [{"key": "a"}]}) == [{"key": "a"}]   # dict-with-posts
    assert _posts_of([{"key": "b"}]) == [{"key": "b"}]              # bare list
    assert _posts_of({"other": 1}) == []                           # dict without posts -> empty, no crash
