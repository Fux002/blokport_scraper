"""Emit writes every variety Name and alias by its field's rule (field-kinds.json; plan W2, W3). A Name is written by
_write_name; a variety's aliases come from the alias stores and are written, split and their drops reported by
_alias_rows (P2-10 C4, D1/D3), never read from a file's Aliases column."""

from __future__ import annotations

import pytest

from stone_pipeline.core.text import UnfoldableName
from stone_pipeline.stages import emit_catalog


def _row(name):
    return {"Key": "granite__x_1", "Name": name, "Type": "Granite", "Aliases": ""}


def test_an_accented_name_is_written_clean():
    row = _row("S\u00e3o Gabriel")
    emit_catalog._write_name(row)
    assert row["Name"] == "Sao Gabriel"


def test_a_name_with_no_ascii_form_stops_emit_naming_the_key():
    with pytest.raises(UnfoldableName, match="granite__x_1"):
        emit_catalog._write_name(_row("Nero \u9ed1 Marquina"))


def test_aliases_are_split_cleaned_and_their_drops_reported(monkeypatch):
    # a store's value is cleaned into its pieces by the alias rule (C4); the alias kind (Mas, 13 Nov) keeps every
    # script and accent: the CJK half is an alias, an accent twin collapses onto the first spelling, and only a
    # refused alias (mojibake) is dropped and reported
    from stone_pipeline.aliases import stores as alias_stores
    from stone_pipeline.aliases.surfaces import Stores
    seed = {"granite__x_1": (["Wratza Limestone\uff0cin China Stone Market:\u7ef4\u7eb3\u591a", "Ros\u00e9", "rose",
                              "K?r?ehir Nero Picasso", "Black Picasso"], [])}
    monkeypatch.setattr(alias_stores, "scraper_stores", lambda backbone=None: Stores(seed=seed))
    aliases, codes, dropped = emit_catalog._alias_rows([_row("Wratza")], seed_only=False)
    assert aliases["granite__x_1"] == [("Wratza Limestone", "seed"), ("\u7ef4\u7eb3\u591a", "seed"),
                                       ("Ros\u00e9", "seed"), ("Black Picasso", "seed")]
    assert codes["granite__x_1"] == [] and dropped == [("granite__x_1", "K?r?ehir Nero Picasso")]


def test_a_bracketed_alternative_leaves_the_name_and_is_the_seed_s_alias():
    # D3: the Name rule drops the '(alternative)' from the Name; the alternative spelling is the seed's alias (the
    # backbone post), never derived from the Name at emit
    row = _row("Black Galaxy (Star Galaxy)")
    emit_catalog._write_name(row)
    assert (row["Name"], row["Aliases"]) == ("Black Galaxy", "")


def test_the_update_file_is_the_cleaned_subset_of_the_full_rows():
    # W26: the incremental upload is the full file's rows the delta names, the same cleaned objects in full order;
    # a delta row the full gates dropped is not published.
    full = [{"Key": "a", "Name": "Rose"}, {"Key": "b", "Name": "Black"}, {"Key": "c", "Name": "Grey"}]
    delta = [{"Key": "c", "Name": "grey"}, {"Key": "a", "Name": "Rosé"}, {"Key": "gone", "Name": "x"}]
    assert emit_catalog.update_rows(full, delta) == [full[0], full[2]]


def test_a_union_filled_key_equals_the_key_of_its_cleaned_name():
    # W18: union-fill keys a missing-category row from the Name before emit cleans it; the one slug rule (W4)
    # and the identity property of the clean rules make that Key the cleaned Name's Key.
    from stone_pipeline.reference.loaders import field_kinds as fk
    from stone_pipeline.phase1.variety_key import new_key
    for raw in ("São Gabriel", "Valle D’Aosta", "Black & Gold", "Nero  (Spain)"):
        assert new_key("Granite", raw) == new_key("Granite", fk.display(fk.VARIATION_NAME, raw))
