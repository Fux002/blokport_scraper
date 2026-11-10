"""SC-4 (plan SC-3-SC-4-PLAN.md section 2a): ONE parser reads a variety Key, `{type slug}__{name slug}_{uuid}`, with
one split and no type list (PHASE-1-SPEC 2a), and every reader goes through it: the type comes from the part before
the first `__`, a variety's identity is (type, name) with no category, and a Key of the old prefixed form is refused,
never parsed into a wrong type. The scraper's own variants are the export's `owner = scraper` rows (D5), not a Key
prefix."""
from __future__ import annotations

import csv

import pytest

from stone_pipeline.config import categories
from stone_pipeline.io import medusa_inputs
from stone_pipeline.reference.loaders import identity

DM = "dolomite_marble__calacatta_gold_1c9a"
D = "dolomite__calacatta_gold_1c9a"


def test_the_type_is_the_part_before_the_first_separator_whatever_the_type_list():
    assert identity.type_slug_from_key(DM) == "dolomite_marble"      # a type slug that prefixes another resolves whole
    assert identity.type_slug_from_key(D) == "dolomite"


@pytest.mark.parametrize("key", ["slab_marble_carrara_8f14e45f", "marble___x_1a", "", "__carrara_8f"])
def test_a_key_not_of_the_new_form_is_refused_never_parsed_to_a_guessed_type(key):
    with pytest.raises(ValueError):
        identity.type_slug_from_key(key)


def test_a_variety_s_identity_is_its_type_and_name_with_no_category():
    assert identity.variety_identity(DM, "Calacatta Gold") == ("dolomite_marble", "calacatta_gold")
    assert identity.variety_identity(DM, "Calacatta Gold") != identity.variety_identity(D, "Calacatta Gold")


def test_the_survivor_prefers_the_seed_then_the_cleanest_name_slug():
    clean, laden = "granite__verde_ubatuba_u1", "granite__verde_ubatuba_ubatuba_u2"
    assert identity.survivor_of([laden, clean], set()) == clean
    assert identity.survivor_of([laden, clean], {laden}) == laden


def test_no_key_names_a_category_any_more():
    assert not hasattr(categories, "category_for_key")


def _export(path, header, rows):
    with path.open("w", encoding="utf-8", newline="") as h:
        csv.writer(h).writerows([header, *rows])


def test_the_scraper_s_own_variants_are_the_export_s_owner_scraper_rows(tmp_path):
    path = tmp_path / "variants_export.csv"
    _export(path, ["Id", "Key", "Name", "owner"],
            [["v1", DM, "Calacatta Gold", "scraper"], ["v2", "np-diesel-en590", "Diesel", ""]])
    assert medusa_inputs.own_variants(path) == 1
    with path.open(encoding="utf-8") as h:
        assert [r["Key"] for r in csv.DictReader(h)] == [DM]


def test_an_owner_value_other_than_scraper_or_empty_is_refused(tmp_path):
    path = tmp_path / "variants_export.csv"
    _export(path, ["Id", "Key", "Name", "owner"], [["v1", DM, "Calacatta Gold", "vendor"]])
    with pytest.raises(ValueError, match="owner"):
        medusa_inputs.own_variants(path)


def test_the_base_carries_no_owner_column_and_is_every_row_the_scraper_s(tmp_path):
    path = tmp_path / "variants_export_base.csv"
    _export(path, ["Key", "Name", "Type", "Aliases"], [[DM, "Calacatta Gold", "Dolomite Marble", ""]])
    assert medusa_inputs.own_variants(path) == 0
