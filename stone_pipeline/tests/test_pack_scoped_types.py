"""The scraper's type vocabulary is its PACK's types (table 569; Mas's ruling, 12 Nov).

BlokPort trades every commodity, so its live attributes.csv also lists types like Steel, Wheat and Wine. Taken as
stone types they made 'Dark Steel', 'Steel Grey', 'Summer Wheat', 'Wild Wine' and 'Wine Red' read as mistyped
stones (adapters/tokens.explicit_type_word into identity.is_mistyped_variant), so catalog.write_variants_to_delete
listed them and drop_deleted_variants cut them from 1_variants_full: on blokport-dev they were never served.

The export now carries `category_handles` on every type row: the '|'-joined product_category handles (D11) the type
has a valid combination in, '' when it has none. A type is the PACK's iff (i) its list holds one of the pack's
category handles, OR (ii) the committed seed (backbone_varieties.json, read at runtime) holds a variety of that type.
Everything else is EXCLUDED, never guessed, and LISTED in the run's review/<env>/types_outside_pack.csv. A seed
variety whose type BE lists with ONLY non-pack handles fails loud, naming the type (the seed and BE disagree). Both
facts are read live; no type list lives in code or config. An excluded type is neither a type word nor a vocabulary
word and never resolves. An attributes.csv without the column is refused. Scoped in ONE place,
reference.loaders.load_attributes, so every reader of the type vocabulary gets the pack's set. Hermetic: a tmp
attributes.csv against the committed seed (its types include Granite, Marble, Gneiss and Slate; never Steel, Wine,
Dolomite Marble or Novelite). No em dashes.
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
    "type,Marble,t_marble,active,false,\n"                      # a seed type, no combination yet: the pack's
    "type,Gneiss,t_gneiss,active,false,\n"
    "type,Steel,t_steel,active,false,steel-coils\n"              # only a foreign category, no seed variety
    "type,Wine,t_wine,active,false,bulk-wine\n"
    "type,Slate,t_slate,active,false,bulk-wine|slabs\n"          # one pack category is enough
    "type,Dolomite Marble,t_dm,active,false,\n"                  # no combination, no seed variety: excluded
    "type,Novelite,t_nov,active,false,slabs\n"                   # new, no seed variety: enters by its combination
    "color,Grey,c_grey,active,false,\n"
    "color,Red,c_red,active,false,\n"
    "category,Slabs,pcat_s,active,false,\n"
    "category,Blocks,pcat_b,active,false,\n"
    "category,Tiles,pcat_t,active,false,\n"
)
DARK_STEEL = "granite__dark_steel_0aff6871-0874-50f4-a814-169e6a961135"


def _file(tmp_path, rows: str) -> Path:
    path = tmp_path / "attributes.csv"
    path.write_text(HEADER + rows, encoding="utf-8")
    return path


@pytest.fixture
def attributes(tmp_path, monkeypatch) -> Path:
    """A tmp attributes.csv, read wherever the vocabulary is loaded (tokens import load_attributes at call time)."""
    path = _file(tmp_path, ROWS)
    real = loaders.load_attributes
    monkeypatch.setattr(loaders, "load_attributes", lambda p=None: real(p or path))
    return path


def test_the_pack_s_types_are_its_handles_and_its_seed(attributes):
    attrs = loaders.load_attributes(attributes)
    assert sorted(attrs.canonical_names("type")) == ["Gneiss", "Granite", "Marble", "Novelite", "Slate"]
    assert attrs.resolve_id("type", "Granite") == ("Granite", "t_granite")


def test_cold_start_every_list_empty_the_seed_s_types_are_the_pack_s(tmp_path):
    rows = "".join(f"type,{t},t_{i},active,false,\n" for i, t in
                   enumerate(["Granite", "Marble", "Gneiss", "Slate", "Steel", "Wine", "Dolomite Marble"]))
    assert sorted(loaders.load_attributes(_file(tmp_path, rows)).canonical_names("type")) == [
        "Gneiss", "Granite", "Marble", "Slate"]


def test_steel_and_wine_are_excluded(attributes):
    attrs = loaders.load_attributes(attributes)
    assert attrs.resolve_id("type", "Steel") is None and attrs.resolve_id("type", "Wine") is None


def test_a_new_type_with_no_seed_variety_enters_at_its_first_pack_category_combination(attributes):
    assert loaders.load_attributes(attributes).resolve_id("type", "Novelite") == ("Novelite", "t_nov")


def test_dolomite_marble_with_no_seed_variety_and_no_combination_is_excluded_never_guessed(attributes):
    assert loaders.load_attributes(attributes).resolve_id("type", "Dolomite Marble") is None
    assert "Dolomite Marble" not in tokens.known_values("type")


def test_a_seed_type_be_lists_only_outside_the_pack_fails_loud_naming_it(tmp_path):
    path = _file(tmp_path, "type,Granite,t_granite,active,false,steel-coils\n")
    with pytest.raises(ValueError, match="Granite"):
        loaders.load_attributes(path)
    with pytest.raises(ValueError, match="Granite"):
        loaders.types_outside_pack(path)


def test_every_excluded_type_is_listed_with_its_reason(attributes, tmp_path, monkeypatch):
    assert loaders.types_outside_pack(attributes) == [
        {"type": "Dolomite Marble", "category_handles": "", "reason": "no_combination"},
        {"type": "Steel", "category_handles": "steel-coils", "reason": "other_categories"},
        {"type": "Wine", "category_handles": "bulk-wine", "reason": "other_categories"},
    ]
    review = tmp_path / "review"
    monkeypatch.setattr(catalog, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(review_dir=review,
                                                                                   attributes_csv=attributes)))
    assert catalog.write_types_outside_pack() == 3
    assert (review / "types_outside_pack.csv").read_text(encoding="utf-8-sig").splitlines() == [
        "type,category_handles,reason", "Dolomite Marble,,no_combination", "Steel,steel-coils,other_categories",
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


def test_the_seed_s_types_are_read_once_per_backbone_file_version(tmp_path, monkeypatch):
    # reviewer 2's L1: the config server loads the vocabulary per request (decide, GET /types); parsing the 12,156-post
    # backbone on each was that request's cost. Read once per file version (path, size, mtime), as C3's seed is.
    import json
    import os
    from stone_pipeline.reference.loaders import backbone
    seed = tmp_path / "backbone_varieties.json"
    seed.write_text(json.dumps({"posts": [{"key": "marble__a_1", "variant": "A", "stone_type": "Marble"}]}),
                    encoding="utf-8")
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(backbone_json=seed)))
    reads, real = [], backbone.read_posts
    monkeypatch.setattr(backbone, "read_posts", lambda path: reads.append(path) or real(path))
    path = _file(tmp_path, "type,Marble,t_marble,active,false,\ntype,Gneiss,t_gneiss,active,false,\n")
    assert loaders.load_attributes(path).canonical_names("type") == ["Marble"]
    assert loaders.load_attributes(path).canonical_names("type") == ["Marble"]
    assert loaders.types_outside_pack(path)[0]["type"] == "Gneiss"
    assert len(reads) == 1
    seed.write_text(json.dumps({"posts": [{"key": "marble__a_1", "variant": "A", "stone_type": "Marble"},
                                          {"key": "gneiss__b_2", "variant": "B", "stone_type": "Gneiss"}]}),
                    encoding="utf-8")
    os.utime(seed, ns=(1, 1))                                     # a new version even on a coarse-mtime filesystem
    assert sorted(loaders.load_attributes(path).canonical_names("type")) == ["Gneiss", "Marble"]
    assert len(reads) == 2


def test_a_seed_the_pack_names_but_does_not_ship_fails_loud_naming_it(tmp_path, monkeypatch):
    # the manager's rule (reviewer 2's LOW): a pack whose yaml names a backbone_file must have it; reading the absent
    # file as "no seed types" would quietly shrink stone's vocabulary on a broken image
    missing = tmp_path / "backbone_varieties.json"
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(backbone_json=missing)))
    with pytest.raises(FileNotFoundError, match="backbone_varieties.json"):
        loaders.load_attributes(_file(tmp_path, ROWS))


def test_a_pack_that_names_no_seed_has_only_its_combinations_types(tmp_path, monkeypatch):
    # ...and a pack naming none (lime) has no seed variety, so rule (ii) adds no type and no backbone is read
    import dataclasses
    from stone_pipeline.config import domain
    from stone_pipeline.reference.loaders import attributes as attributes_mod, backbone
    seedless = dataclasses.replace(domain.active_pack(), backbone_file=None)
    monkeypatch.setattr(domain, "active_pack", lambda: seedless)
    monkeypatch.setattr(attributes_mod, "active_pack", lambda: seedless)
    monkeypatch.setattr(backbone, "read_posts", lambda path: pytest.fail(f"read the backbone {path}"))
    rows = "type,Granite,t_granite,active,false,blocks|slabs|tiles\ntype,Marble,t_marble,active,false,\n"
    assert loaders.load_attributes(_file(tmp_path, rows)).canonical_names("type") == ["Granite"]
