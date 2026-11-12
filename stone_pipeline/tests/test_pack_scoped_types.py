"""The scraper's type vocabulary is its PACK's types (table 569; the scraper manager, 11-12).

BlokPort trades every commodity, so its live attributes.csv also lists types like Steel, Wheat and Wine. Taken as
stone types they made 'Dark Steel', 'Steel Grey', 'Summer Wheat', 'Wild Wine' and 'Wine Red' read as mistyped
stones (adapters/tokens.explicit_type_word into identity.is_mistyped_variant), so catalog.write_variants_to_delete
listed them and drop_deleted_variants cut them from 1_variants_full: on blokport-dev they were never served.

The export now carries `category_handles` on every type row: the '|'-joined product_category handles (D11) the type
has a valid combination in, '' when it has none. A type is the PACK's iff its list holds one of the pack's category
handles (Mas's 569 clause, 12 Nov): a type with only other categories, or with none yet, is EXCLUDED, never guessed,
and LISTED in the run's review/<env>/types_outside_pack.csv. A new stone type enters the pack with its first
stone-category combination, made on the BE side. An excluded type is neither a type word nor a vocabulary word and
never resolves. An attributes.csv without the column is refused. Scoped in ONE place,
reference.loaders.load_attributes, so every reader of the type vocabulary gets the pack's set. Hermetic: a tmp
attributes.csv. No em dashes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from stone_pipeline import catalog
from stone_pipeline.adapters import tokens
from stone_pipeline.reference import loaders
from stone_pipeline.reference.medusa_files import read_attributes

HEADER = "category,value,sourceid,status,internal,category_handles\n"
ROWS = (
    "type,Granite,t_granite,active,false,blocks|slabs|tiles\n"
    "type,Marble,t_marble,active,false,blocks|slabs|tiles\n"
    "type,Gneiss,t_gneiss,active,false,\n"                       # no combination yet: excluded, listed
    "type,Steel,t_steel,active,false,steel-coils\n"              # only a foreign category: foreign
    "type,Wine,t_wine,active,false,bulk-wine\n"
    "type,Slate,t_slate,active,false,bulk-wine|slabs\n"          # one pack category is enough
    "color,Grey,c_grey,active,false,\n"
    "color,Red,c_red,active,false,\n"
    "category,Slabs,pcat_s,active,false,\n"
    "category,Blocks,pcat_b,active,false,\n"
    "category,Tiles,pcat_t,active,false,\n"
)
DARK_STEEL = "granite__dark_steel_0aff6871-0874-50f4-a814-169e6a961135"


@pytest.fixture
def attributes(tmp_path, monkeypatch) -> Path:
    """A tmp attributes.csv, read wherever the vocabulary is loaded (tokens import load_attributes at call time)."""
    path = tmp_path / "attributes.csv"
    path.write_text(HEADER + ROWS, encoding="utf-8")
    real = loaders.load_attributes
    monkeypatch.setattr(loaders, "load_attributes", lambda p=None: real(p or path))
    return path


def test_a_type_only_in_foreign_categories_is_not_a_pack_type(attributes):
    attrs = loaders.load_attributes(attributes)
    assert sorted(attrs.canonical_names("type")) == ["Granite", "Marble", "Slate"]
    assert attrs.resolve_id("type", "Steel") is None and attrs.resolve_id("type", "Wine") is None
    assert attrs.resolve_id("type", "Granite") == ("Granite", "t_granite")


def test_a_type_with_no_combination_yet_is_excluded_never_guessed(attributes):
    assert loaders.load_attributes(attributes).resolve_id("type", "Gneiss") is None
    assert "gneiss" not in tokens._clear_type_words() and "Gneiss" not in tokens.known_values("type")


def test_every_excluded_type_is_listed_with_its_reason(attributes, tmp_path, monkeypatch):
    assert loaders.types_outside_pack(attributes) == [
        {"type": "Gneiss", "category_handles": "", "reason": "no_combination"},
        {"type": "Steel", "category_handles": "steel-coils", "reason": "other_categories"},
        {"type": "Wine", "category_handles": "bulk-wine", "reason": "other_categories"},
    ]
    review = tmp_path / "review"
    monkeypatch.setattr(catalog, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(review_dir=review,
                                                                                   attributes_csv=attributes)))
    assert catalog.write_types_outside_pack() == 3
    assert (review / "types_outside_pack.csv").read_text(encoding="utf-8-sig").splitlines() == [
        "type,category_handles,reason", "Gneiss,,no_combination", "Steel,steel-coils,other_categories",
        "Wine,bulk-wine,other_categories"]


def test_a_type_with_one_pack_category_among_foreign_ones_is_a_pack_type(attributes):
    assert loaders.load_attributes(attributes).resolve_id("type", "Slate") == ("Slate", "t_slate")


def test_a_foreign_type_is_neither_a_type_word_nor_a_vocabulary_word(attributes):
    assert "Steel" not in tokens.known_values("type") and "Wine" not in tokens.known_values("type")
    assert {"steel", "wine"}.isdisjoint(tokens._clear_type_words())
    assert "granite" in tokens._clear_type_words()                         # a pack type is still a type word
    assert tokens.explicit_type_word("Dark Steel") is None
    assert tokens.explicit_type_word("Wine Red") is None
    assert tokens.explicit_type_word("Azul White Granite") == "Granite"


def test_dark_steel_granite_is_not_mistyped_and_stays_off_the_delete_list(attributes, tmp_path, monkeypatch):
    assert loaders.is_mistyped_variant(DARK_STEEL, "Dark Steel") is False
    export = tmp_path / "variants_export.csv"
    export.write_text("Id,Key,Name,Aliases,Type\n"
                      f"01DARK,{DARK_STEEL},Dark Steel,,Granite\n"
                      "01AZUL,onyx__azul_white_granite_1,Azul White Granite,,Onyx\n", encoding="utf-8")
    review = tmp_path / "review"
    monkeypatch.setattr(catalog, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(export_file=export,
                                                                                   review_dir=review)))
    catalog.write_variants_to_delete()
    listed = (review / "variants_to_delete.csv").read_text(encoding="utf-8-sig")
    assert DARK_STEEL not in listed
    assert "onyx__azul_white_granite_1" in listed                            # a real mistype is still listed


def test_an_attributes_file_without_category_handles_is_refused(tmp_path):
    path = tmp_path / "attributes.csv"
    path.write_text("category,value,sourceid,status,internal\ntype,Granite,t_granite,active,false\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="category_handles"):
        loaders.load_attributes(path)


def test_the_fetch_refuses_an_attributes_download_without_category_handles(tmp_path):
    # at the boundary: a download without the column never replaces the local file (medusa_inputs.check_readable)
    from stone_pipeline.io import medusa_inputs
    old = tmp_path / "old.csv"
    old.write_text("category,value,sourceid,status,internal\ntype,Granite,t_granite,active,false\n", encoding="utf-8")
    with pytest.raises(ValueError, match="category_handles"):
        medusa_inputs.check_readable("attributes.csv", old)
    good = tmp_path / "good.csv"
    good.write_text(HEADER + ROWS, encoding="utf-8")
    medusa_inputs.check_readable("attributes.csv", good)


_REPO = Path(__file__).resolve().parents[2]


def test_the_committed_dev_attributes_carry_blokport_dev_s_real_handles():
    # SOURCE (never inferred): P1X's read of blokport-dev, full-model-plan/569-type-categories/runs/
    # 55988743-blokport-dev-20261112T1307Z.out (sha256 1d2bba1f582489752b2709835a86d7b48b1c974df4f17f38442381a50d3044a2):
    # 30 stone types in blocks|slabs|tiles, and Dolomite Marble with no live combination.
    path = _REPO / "from_medusa" / "development" / "attributes.csv"
    types = {v.value: v.category_handles for v in read_attributes(path, require_status=False,
                                                                     require_category_handles=True)
             if v.category == "type"}
    assert len(types) == 31 and types.pop("Dolomite Marble") == ()
    assert set(types.values()) == {("blocks", "slabs", "tiles")}
    assert loaders.types_outside_pack(path) == [
        {"type": "Dolomite Marble", "category_handles": "", "reason": "no_combination"}]
    assert len(loaders.load_attributes(path).canonical_names("type")) == 30


def test_the_wood_fixture_s_handles_are_synthetic_test_data():
    # SYNTHETIC: the wood fixture is test data, not an export; its types carry the wood pack's own three handles
    path = _REPO / "stone_pipeline" / "tests" / "fixtures" / "wood" / "workspace" / "from_medusa" / "development" \
        / "attributes.csv"
    handles = {v.category_handles for v in read_attributes(path, require_status=False, require_category_handles=True)
               if v.category == "type"}
    assert handles == {("beams", "panels", "planks")}
