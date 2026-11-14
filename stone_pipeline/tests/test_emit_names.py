"""Emit writes every variety Name and alias by its field's rule (field-kinds.json)."""

from __future__ import annotations

import pytest

from stone_pipeline.core.text import UnfoldableName
from stone_pipeline.stages import emit_catalog


def _row(name, aliases=""):
    return {"Key": "granite__x_1", "Name": name, "Aliases": aliases}


def test_an_accented_name_is_written_clean():
    row = _row("S\u00e3o Gabriel")
    assert emit_catalog._write_names(row) == []
    assert row["Name"] == "Sao Gabriel"


def test_a_name_with_no_ascii_form_stops_emit_naming_the_key():
    with pytest.raises(UnfoldableName, match="granite__x_1"):
        emit_catalog._write_names(_row("Nero \u9ed1 Marquina"))


def test_aliases_are_split_cleaned_and_their_drops_reported():
    # the alias kind keeps every script and accent: the CJK half is an alias, nothing is dropped, and an accent twin
    # collapses onto the first spelling
    row = _row("Wratza", "Wratza Limestone\uff0cin China Stone Market:\u7ef4\u7eb3\u591a|Ros\u00e9|rose")
    assert emit_catalog._write_names(row) == []
    assert row["Aliases"] == "Wratza Limestone|\u7ef4\u7eb3\u591a|Ros\u00e9"
    # a refused alias (mojibake) is still dropped and reported
    row = _row("Nero Picasso", "K?r?ehir Nero Picasso|Black Picasso")
    assert emit_catalog._write_names(row) == ["K?r?ehir Nero Picasso"]
    assert row["Aliases"] == "Black Picasso"


def test_a_bracketed_alternative_moves_from_the_name_into_the_aliases():
    row = _row("Black Galaxy (Star Galaxy)")
    emit_catalog._write_names(row)
    assert (row["Name"], row["Aliases"]) == ("Black Galaxy", "Star Galaxy")


def test_the_update_file_is_the_cleaned_subset_of_the_full_rows():
    # the incremental upload is the full file's rows the delta names, the same cleaned objects in full order;
    # a delta row the full gates dropped is not published.
    full = [{"Key": "a", "Name": "Rose"}, {"Key": "b", "Name": "Black"}, {"Key": "c", "Name": "Grey"}]
    delta = [{"Key": "c", "Name": "grey"}, {"Key": "a", "Name": "Rosé"}, {"Key": "gone", "Name": "x"}]
    assert emit_catalog.update_rows(full, delta) == [full[0], full[2]]


def test_a_union_filled_key_equals_the_key_of_its_cleaned_name():
    # union-fill keys a missing-category row from the Name before emit cleans it; the one slug rule and the identity
    # property of the clean rules make that Key the cleaned Name's Key.
    from stone_pipeline.reference.loaders import field_kinds as fk
    from stone_pipeline.phase1.variety_key import new_key
    for raw in ("São Gabriel", "Valle D’Aosta", "Black & Gold", "Nero  (Spain)"):
        assert new_key("Granite", raw) == new_key("Granite", fk.display(fk.VARIATION_NAME, raw))
