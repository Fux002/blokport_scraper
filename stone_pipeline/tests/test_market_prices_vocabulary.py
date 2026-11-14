"""Each brand's scope vocabulary DECLARES which listings its runs keep, and the collector asks one question of it,
Vocabulary.in_scope(listing_text, market), after the fetch, for every platform.

- `scope: all` keeps every listing the collector reads: BlokPort declares it, so its runs keep exactly what they keep
  today, by a visible declaration, never a missing-file branch.
- `scope: words` keeps a listing whose text holds a word of a category in its market's languages: WudPort's sheet
  materials in the sellers' own words. Text and words compare after core.text.match_key (case, accents and punctuation
  do not count) and a word may sit inside a longer word (a compound: "Birkenmultiplex", "bjorkplywood").
- Every category names every language its markets use (an empty list is a stated gap, never a silent omission); a
  market the vocabulary does not declare fails loud.
Red form: names the fix adds are read with a default. No em dashes."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from market_prices import vocabulary as vocab
from market_prices.vocabulary import VocabularyError, load_vocabulary

# the sheet-material categories the research covered
SHEET_CATEGORIES = {"plywood", "film_faced_plywood", "packing_plywood", "mdf", "osb", "chipboard",
                    "hollow_core_chipboard", "lvl", "veneer", "core_veneer", "hdf_door_skin", "hardboard"}
# the WudPort sites' markets and their languages
WUDPORT_MARKETS = {"GB": ("en",), "DE": ("de",), "FR": ("fr",), "NL": ("nl",), "BE": ("nl", "fr"), "PL": ("pl",),
                   "SE": ("sv",)}


def _write(directory: Path, document: object) -> Path:
    (directory / f"{document['brand']}.yaml").write_text(yaml.safe_dump(document, allow_unicode=True),
                                                        encoding="utf-8")
    return directory


def _words(**by_language) -> dict:
    return {"brand": "wudport", "scope": "words", "languages_by_market": {"GB": ["en"], "DE": ["de"], "PL": ["pl"]},
            "categories": [{"category": "sheet", "words": {"en": [], "de": [], "pl": [], **by_language}}]}


# --- the committed files --------------------------------------------------------------------------------------------
def test_blokport_declares_that_its_runs_keep_every_listing():
    blokport = load_vocabulary("blokport")
    assert getattr(blokport, "scope", None) == "all"
    assert blokport.in_scope("Bianco Carrara 2 cm polished", "BE")


def test_wudport_declares_the_sheet_material_categories_in_its_markets_languages():
    wudport = load_vocabulary("wudport")
    assert getattr(wudport, "scope", None) == "words"
    assert {market: tuple(langs) for market, langs in wudport.languages_by_market.items()} == WUDPORT_MARKETS
    assert {c.category for c in wudport.categories} == SHEET_CATEGORIES
    assert wudport.in_scope("Film Faced Plywood 18mm 2440 x 1220", "GB")
    assert wudport.in_scope("Siebdruckplatte Birke 18 mm", "DE")
    assert wudport.in_scope("Płyta wiórowa surowa 18 mm", "PL")
    assert not wudport.in_scope("Bianco Carrara 2 cm polished", "BE")


# --- the rule ---------------------------------------------------------------------------------------------------------
def test_a_word_matches_folded_and_inside_a_compound(tmp_path):
    v = load_vocabulary("wudport", _write(tmp_path, _words(en=["film faced plywood"], de=["Multiplex"],
                                                            pl=["płyta wiórowa"])))
    assert v.in_scope("FILM-FACED Plywood, 18 mm", "GB")
    assert v.in_scope("Birkenmultiplex 18 mm", "DE")
    assert v.in_scope("Plyta wiorowa laminowana", "PL")
    assert not v.in_scope("Paint roller 9 inch", "GB")


def test_a_word_scopes_only_its_own_languages_markets(tmp_path):
    v = load_vocabulary("wudport", _write(tmp_path, _words(de=["Spanplatte"])))
    assert v.in_scope("Spanplatte roh 19 mm", "DE")
    assert not v.in_scope("Spanplatte roh 19 mm", "GB")


def test_a_market_the_vocabulary_does_not_declare_fails_loud(tmp_path):
    v = load_vocabulary("wudport", _write(tmp_path, _words(en=["plywood"])))
    with pytest.raises(VocabularyError, match="US"):
        v.in_scope("Plywood", "US")


@pytest.mark.parametrize("document, message", [
    ({"brand": "wudport"}, "scope"),
    ({"brand": "wudport", "scope": "all", "languages_by_market": {"GB": ["en"]}}, "all"),
    ({"brand": "wudport", "scope": "words", "languages_by_market": {"GB": ["en"]}, "categories": []}, "categories"),
    ({"brand": "wudport", "scope": "words", "languages_by_market": {"GB": ["en"], "DE": ["de"]},
      "categories": [{"category": "sheet", "words": {"en": ["plywood"]}}]}, "de"),
    ({"brand": "wudport", "scope": "words", "languages_by_market": {"GB": ["en"]},
      "categories": [{"category": "sheet", "words": {"en": ["Plywood", "plywood"]}}]}, "plywood"),
])
def test_a_vocabulary_that_is_not_a_whole_declaration_is_refused(tmp_path, document, message):
    with pytest.raises(VocabularyError, match=message):
        load_vocabulary("wudport", _write(tmp_path, document))


def test_an_empty_word_list_is_a_stated_gap(tmp_path):
    v = load_vocabulary("wudport", _write(tmp_path, _words(en=["plywood"])))
    assert not v.in_scope("Sklejka 18 mm", "PL")
    assert getattr(vocab, "Vocabulary") is type(v)
