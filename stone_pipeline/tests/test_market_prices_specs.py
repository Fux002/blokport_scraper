"""market_prices contract v3: a listing's thickness, finish, material and origin as normalised columns (CONTRACT.md
"Specs", pinned for the pricing module at ae0f2116). Each source maps its own labels (a register row's spec_labels, a
sheet layout's specs); words map through the brand's vocabulary, countries through one brand-neutral table; a stated
value with no honest mapping refuses its row with a named reason, never a guess. Fixtures only."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from market_prices import config
from market_prices.schema import MalformedListing, Observation, columns
from market_prices.vocabulary import Vocabulary, load_vocabulary

REPO = Path(__file__).resolve().parents[2]


def _specs():
    """market_prices.specs, imported per test: a tree without it fails each test for that reason, never the whole
    collection (so a red run shows every test failing on its own)."""
    return importlib.import_module("market_prices.specs")

V4_HEADER = (
    "contract_version,source,brand,site_id,seller_name,seller_type,seller_country,served_market,"
    "seller_is_our_supplier,supplier_key,listing_key,source_url,listing_text,stated_specs,thickness_mm,finish,"
    "material_type,origin_iso_2,thickness_text,finish_text,material_text,origin_text,spec_labels,grade_text,"
    "price_text,price_amount,regular_price_text,regular_price_amount,currency,price_status,vat_basis,unit_text,"
    "delivery_text,delivery_included,min_qty_text,"
    "availability_text,provenance,observed_at,run_id,collector_version")


def _vocabulary(**overrides) -> Vocabulary:
    document = {"brand": "blokport", "scope": "all",
                "finishes": {"Polished": ["gepolijst"], "Honed": ["verzoet", "donker verzoet"]},
                "materials": {"Granite": ["graniet"], "Bluestone": ["blauwe hardsteen"]},
                "unstated": ["Not specified", "Not defined in catalog legend"]}
    return Vocabulary.model_validate({**document, **overrides})


def _countries():
    return _specs().Countries.model_validate({"countries": [
        {"iso2": "BE", "iso3": "BEL", "names": ["Belgium"]},
        {"iso2": "NO", "iso3": "NOR", "names": ["Norway"]},
    ]})


def _labels(**overrides):
    document = {"thickness": {"labels": ["Dikte", "Thickness"], "unit": "cm"}, "finish": {"labels": ["Afwerking"]},
                "material_type": {"labels": ["Steensoort"]}, "origin": {"labels": ["Herkomst"]}}
    return _specs().SpecLabels.model_validate({**document, **overrides})


def _read(stated: dict, labels=None, vocabulary: Vocabulary | None = None):
    return _specs().read_specs(stated, labels if labels is not None else _labels(), vocabulary or _vocabulary(),
                               _countries())


# --- the delivered shape ---------------------------------------------------------------------------------------------
def test_the_contract_is_version_4_with_the_pinned_header():
    assert config.CONTRACT_VERSION == "4"
    assert ",".join(columns(Observation)) == V4_HEADER
    assert V4_HEADER in (REPO / "market_prices" / "CONTRACT.md").read_text(encoding="utf-8")


# --- one stated listing, read through its labels ---------------------------------------------------------------------
def test_each_spec_is_read_through_its_label_into_its_column_with_the_words_as_written():
    specs = _read({"Dikte": "2", "Afwerking": "Gepolijst", "Steensoort": "blauwe hardsteen", "Herkomst": "Belgium",
                   "Kleur": "Blauw"})
    assert (specs.thickness_mm, specs.finish, specs.material_type, specs.origin_iso_2) == (
        "20", "Polished", "Bluestone", "be")
    assert specs.texts == {"thickness_mm": "2", "finish": "Gepolijst", "material_type": "blauwe hardsteen",
                           "origin_iso_2": "Belgium"}
    assert specs.labels == {"thickness_mm": "Dikte", "finish": "Afwerking", "material_type": "Steensoort",
                            "origin_iso_2": "Herkomst"}
    assert specs.used == {"Dikte", "Afwerking", "Steensoort", "Herkomst"}         # "Kleur" stays in stated_specs


def test_labels_compare_case_and_spacing_aside_and_an_unlabelled_spec_is_empty():
    specs = _read({"DIKTE": "3 cm"})
    assert (specs.thickness_mm, specs.labels, specs.finish, specs.origin_iso_2) == ("30", {"thickness_mm": "DIKTE"},
                                                                                   None, None)


def test_a_source_with_no_labels_yet_states_nothing():
    specs = _specs().read_specs({"Dikte": "2"}, None, _vocabulary(), _countries())
    assert (specs.thickness_mm, specs.texts, specs.labels, specs.used, specs.unstated) == (None, {}, {}, set(), 0)


@pytest.mark.parametrize("stated, unit, millimetres", [
    ("2", "cm", "20"), ("1.8", "cm", "18"), ("1,8 cm", None, "18"), ("18 mm", "cm", "18"), ("12.50", "mm", "12.5"),
    ("0.6 cm", None, "6"), ("3CM", None, "30"), ("20", "mm", "20"),
])
def test_a_thickness_is_one_number_in_mm_exact_with_no_trailing_zeros(stated, unit, millimetres):
    labels = _labels(thickness={"labels": ["Dikte"], "unit": unit} if unit else {"labels": ["Dikte"]})
    assert _read({"Dikte": stated}, labels).thickness_mm == millimetres


@pytest.mark.parametrize("stated", ["2-3 cm", "2 x 3 cm", "2 in", "18", "0 mm", "about 2 cm", "2 cm 3 cm"])
def test_a_thickness_that_is_not_one_number_in_mm_or_cm_refuses_the_row(stated):
    with pytest.raises(MalformedListing, match=r"thickness '.*' is not one number in mm or cm"):
        _read({"Dikte": stated}, _labels(thickness={"labels": ["Dikte"]}))


def test_a_word_maps_whole_through_the_vocabulary_never_by_a_part():
    assert _read({"Afwerking": "Donker  Verzoet"}).finish == "Honed"
    assert _read({"Afwerking": "Polished"}).finish == "Polished"                 # the canonical names itself
    with pytest.raises(MalformedListing, match=r"finish 'Gepolijst - mastic' is not in blokport's finishes"):
        _read({"Afwerking": "Gepolijst - mastic"})
    with pytest.raises(MalformedListing, match=r"material 'witsteen' is not in blokport's materials"):
        _read({"Steensoort": "witsteen"})


def test_an_unstated_word_leaves_the_column_empty_counts_it_and_keeps_it_in_stated_specs():
    specs = _read({"Afwerking": "Not specified", "Herkomst": "Not defined in catalog legend", "Dikte": "2"})
    assert (specs.finish, specs.origin_iso_2, specs.unstated) == (None, None, 2)
    assert specs.used == {"Dikte"} and set(specs.labels) == {"thickness_mm"}


@pytest.mark.parametrize("stated, code", [("BE", "be"), ("BEL", "be"), ("Belgium", "be"), ("belgium", "be"),
                                          ("NO", "no"), ("Norway", "no")])
def test_an_origin_is_a_code_as_iso_writes_it_or_a_name_delivered_lower_case(stated, code):
    assert _read({"Herkomst": stated}).origin_iso_2 == code


@pytest.mark.parametrize("stated", ["Middle East (regional designation)", "be", "Belgi", "XX"])
def test_an_origin_not_in_the_country_table_refuses_the_row(stated):
    with pytest.raises(MalformedListing, match=r"origin '.*' is not in the country table"):
        _read({"Herkomst": stated})


def test_two_labels_stating_one_spec_must_agree():
    assert _read({"Dikte": "2", "Thickness": "2"}).thickness_mm == "20"
    with pytest.raises(MalformedListing, match=r"thickness_mm: two labels state different values '2' / '3'"):
        _read({"Dikte": "2", "Thickness": "3"})


# --- the maps' own rules ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("overrides, message", [
    ({"finishes": {"Polished": ["gepolijst"], "Honed": ["Gepolijst"]}}, "more than one"),
    ({"materials": {"Granite": ["graniet"], "Marble": ["graniet"]}}, "more than one"),
    ({"unstated": ["gepolijst"]}, "unstated"),
])
def test_a_word_means_one_thing_in_a_vocabulary(overrides, message):
    with pytest.raises(ValueError, match=message):
        _vocabulary(**overrides)


@pytest.mark.parametrize("document, message", [
    ({"countries": [{"iso2": "BE", "iso3": "BEL", "names": ["Belgium"]},
                    {"iso2": "BE", "iso3": "BLX", "names": ["Belgique"]}]}, "BE"),
    ({"countries": [{"iso2": "BE", "iso3": "BEL", "names": ["Belgium"]},
                    {"iso2": "NL", "iso3": "NLD", "names": ["belgium"]}]}, "belgium"),
    ({"countries": [{"iso2": "be", "iso3": "BEL", "names": ["Belgium"]}]}, "iso2"),
])
def test_the_country_table_names_each_country_and_each_name_once(document, message):
    with pytest.raises(ValueError, match=message):
        _specs().Countries.model_validate(document)


def test_a_spec_map_names_each_spec_with_at_least_one_label():
    with pytest.raises(ValueError):
        _specs().SpecLabels.model_validate({"finish": {"labels": []}})
    with pytest.raises(ValueError):
        _specs().SpecLabels.model_validate({"thickness": {"labels": ["Dikte"], "unit": "inch"}})


# --- the committed data ----------------------------------------------------------------------------------------------
def test_the_committed_country_table_reads_every_code_as_text():
    countries = _specs().load_countries()
    # YAML 1.1 reads a bare NO as false: every code must still be Norway's
    assert (countries.code_for("NO"), countries.code_for("NOR"), countries.code_for("Norway")) == ("no", "no", "no")
    assert countries.code_for("Belgium") == "be"


def test_blokports_materials_are_the_seeds_own_stone_types():
    types = {post["stone_type"] for post in json.loads(
        (REPO / "catalog_source" / "backbone_varieties.json").read_text(encoding="utf-8"))["posts"]}
    finishes = {finish for post in json.loads(
        (REPO / "catalog_source" / "backbone_varieties.json").read_text(encoding="utf-8"))["posts"]
        for finish in post.get("finishes", [])}
    vocabulary = load_vocabulary("blokport")
    assert vocabulary.materials and set(vocabulary.materials) <= types
    assert vocabulary.finishes and set(vocabulary.finishes) <= finishes
