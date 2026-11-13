"""P2-10 C4, the one-off that writes both committed bases' Aliases as emit projects them (regen_base_aliases): only
Aliases cells change, in the file's own byte form, and anything else the projection would change is refused before a
byte is written. That the committed bases are a fixed point is test_cold_start_idempotence's. No em dashes."""
from __future__ import annotations

import pytest

from stone_pipeline.reference import regen_base_aliases as rba

BASE = "Key,Name,Type,Aliases\r\nk1,Casa Blue,Marble,Old\r\nk2,Black Galaxy,Granite,\r\n"


def _full(*aliases, **change):
    rows = [{"Key": "k1", "Name": "Casa Blue", "Type": "Marble"}, {"Key": "k2", "Name": "Black Galaxy", "Type": "Granite"}]
    return [{**r, **change.get(r["Key"], {}), "Aliases": a} for r, a in zip(rows, aliases)]


def test_only_the_aliases_cells_change_in_the_file_s_own_byte_form():
    out = rba.regenerate(BASE, _full("Azul Casa|Casablue", "Galaxy Nero"))
    assert out == "Key,Name,Type,Aliases\r\nk1,Casa Blue,Marble,Azul Casa|Casablue\r\nk2,Black Galaxy,Granite,Galaxy Nero\r\n"
    assert rba.regenerate(BASE.rstrip("\r\n"), _full("Old", "")) == BASE.rstrip("\r\n")    # no final newline kept


def test_a_projection_that_moves_a_key_name_or_type_is_refused():
    for change in ({"k1": {"Key": "k9"}}, {"k2": {"Name": "Galaxy Black"}}, {"k1": {"Type": "Granite"}}):
        with pytest.raises(rba.Refused, match="the projection changes"):
            rba.regenerate(BASE, _full("Old", "", **change))


def test_a_projection_with_another_row_count_is_refused():
    with pytest.raises(rba.Refused, match="1 rows, the base 2"):
        rba.regenerate(BASE, _full("Old"))


def test_the_projection_needs_no_published_field_kinds(tmp_path, monkeypatch):
    # a dispatch runner has no from_medusa/<env>/field-kinds.json (a produce fetches it): the tool reads the committed
    # fixture, the only committed copy of the map (as clean_committed_names does)
    from stone_pipeline.reference.loaders import field_kinds
    from stone_pipeline.stages import emit_catalog
    monkeypatch.setattr(field_kinds, "_path", lambda: tmp_path / "absent" / "field-kinds.json")
    monkeypatch.setattr(emit_catalog, "_alias_rows", lambda rows, seed_only: ({r["Key"]: [] for r in rows},
                                                                             {r["Key"]: [] for r in rows}, []))
    assert [r["Name"] for r in rba.project("Key,Name,Type,Aliases\nk1,casa blue,Marble,\n")] == ["Casa Blue"]


def test_nothing_is_written_when_a_second_pass_would_change_a_base(tmp_path, monkeypatch):
    path = tmp_path / "base.csv"
    path.write_bytes(BASE.encode("utf-8"))
    monkeypatch.setattr(rba, "ROOT", tmp_path)
    monkeypatch.setattr(rba, "BASES", ["base.csv"])
    passes = iter([_full("A", ""), _full("B", "")])                 # the second pass projects other aliases
    monkeypatch.setattr(rba, "project", lambda text: next(passes))
    with pytest.raises(rba.Refused, match="not a fixed point"):
        rba.main()
    assert path.read_bytes() == BASE.encode("utf-8")
