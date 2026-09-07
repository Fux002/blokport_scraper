"""Product-FORM words (pack.product_form_words) and split type spellings in variety names.

A vendor puts a physical form or trade unit in the product title ('Pietra Grey Marble Step', 'White
Travertine Cross-cut Slab'); the form never carries variety identity, so the cleaner strips it exactly
like the category format words, and the alias judge treats single-token forms as generic. A split spelling
of a one-word type ('Black Soap Stone') is read as that type and rewritten to the reference spelling.
Every rule is pack-driven: a pack that declares no forms strips nothing extra.
"""
import dataclasses

import pytest

from stone_pipeline.adapters import tokens
from stone_pipeline.adapters.tokens import clean_variety, explicit_type_word, strip_format, strip_forms
from stone_pipeline.config.domain import active_pack
from stone_pipeline.matching.alias_resolver import AliasResolver
from stone_pipeline.reference.loaders import load_attributes


# --- the cleaner strips product forms -------------------------------------------------------------------

def test_form_word_is_stripped_and_identity_binds_to_the_existing_variety():
    # 'Step' is a form, not part of the name; the identity is the existing variety Pietra Grey.
    assert clean_variety("Pietra Grey Marble Step", "Marble") == "Pietra Grey"


def test_cut_orientation_is_stripped_hyphen_or_space():
    assert clean_variety("White Travertine Cross-cut Slab", "Travertine") == "White Travertine"
    assert clean_variety("White Travertine Cross cut Slab", "Travertine") == "White Travertine"
    assert clean_variety("White Travertine Crosscut Slab", "Travertine") == "White Travertine"


def test_strip_forms_is_whole_word_and_case_insensitive():
    assert strip_forms("Pietra Grey STEP") == "Pietra Grey"
    # 'Stepping' is not 'step': word boundaries protect a longer word
    assert strip_forms("Grey Stepping") == "Grey Stepping"


def test_strip_format_strips_forms_too():
    # marenostone's variety_match_key path (strip_format) must not carry a form into the exact tier
    assert strip_format("Pietra Grey Marble Step") == "Pietra Grey Marble"


def test_unrelated_name_is_unchanged():
    assert clean_variety("Crystal White Granite Slab", "Granite") == "Crystal White"


# --- collision safety: words inside REAL variety names are never stripped ---------------------------------

@pytest.mark.parametrize("name", ["Silver Rock", "White Mosaic", "Blue Vein", "Savanna Paver", "Cap Star",
                                  "Gravel Gray", "Gold Brick Grey", "White Hearth"])
def test_words_that_occur_in_real_variety_names_are_not_form_words(name):
    assert strip_forms(name) == name


def test_bare_vein_is_never_a_form_word_only_the_compound_is():
    forms = active_pack().product_form_words
    assert "vein" not in forms and "cross" not in forms and "cut" not in forms
    assert {"vein-cut", "vein cut", "veincut", "cross-cut", "cross cut", "crosscut"} <= forms


def test_form_words_do_not_collide_with_attribute_vocabularies():
    # a form word must never equal a finish/colour/type value ('Carved' is a finish, not a form)
    attrs = load_attributes()
    vocab = {tokens.match_key(v) for cat in ("finish", "color", "type") for v in attrs.canonical_names(cat)}
    forms = {tokens.match_key(w) for w in active_pack().product_form_words}
    assert not (forms & vocab), forms & vocab


# --- the alias judge: single-token forms are generic, phrase parts never leak --------------------------------

def test_form_word_difference_is_an_alias():
    d = AliasResolver().decide("Pietra Grey Step", "Marble", None, "Pietra Grey", "Marble", None)
    assert d.verdict == "alias"


def test_phrase_parts_do_not_leak_into_the_generic_set():
    # 'vein cut' is a phrase entry; its parts 'vein'/'cut' must not become generic, or 'White Vein Cut'
    # would alias onto 'White' and 'Blue Vein' onto 'Blue'.
    generic = AliasResolver()._generic_set()
    assert "step" in generic
    assert "vein" not in generic and "cut" not in generic and "cap" not in generic and "wall" not in generic


# --- split type spellings: 'Soap Stone' is the type Soapstone ------------------------------------------------

def test_split_type_spelling_is_read_as_the_type_at_the_end_or_start():
    assert explicit_type_word("Black Soap Stone") == "Soap Stone"
    assert explicit_type_word("Soap Stone Black") == "Soap Stone"


def test_split_type_spelling_resolves_to_the_canonical_type():
    canonical, _ = load_attributes().resolve_id("type", "Soap Stone")
    assert canonical == "Soapstone"


def test_split_type_spelling_two_token_name_is_a_variety_named_after_the_type():
    assert explicit_type_word("Soap Stone") is None


def test_negated_split_type_spelling_is_not_the_type():
    assert explicit_type_word("Falsa Soap Stone") is None


def test_clean_variety_rewrites_split_spelling_to_the_reference_spelling():
    # the existing variety is 'Black Soapstone' (soapstone); the vendor mis-tags it Granite and spells the
    # type as two words -- the identity still keys on the reference spelling
    assert clean_variety("Black Soap Stone", "Granite") == "Black Soapstone"
    assert clean_variety("Black Soap Stone", "Soapstone") == "Black Soapstone"


def test_single_word_type_rules_are_unchanged():
    assert explicit_type_word("Azul White Quartzite") == "Quartzite"
    assert explicit_type_word("Onyx Blue") == "Onyx"


def test_trailing_type_word_beats_a_leading_split_spelling():
    # 'Blue Stone Basalt' IS a basalt (3 committed varieties); a leading 'Blue Stone' is a colour+material
    # descriptor, never the type Bluestone. The trailing single type word must always win.
    assert explicit_type_word("Blue Stone Basalt") == "Basalt"


def test_blue_stone_is_deliberately_not_a_split_type_spelling():
    # 'blue stone' occurs inside real variety names as a descriptor, so it is excluded from the type synonyms
    # (adding it re-typed the committed 'Blue Stone Basalt' rows -- the regression this guards).
    assert "blue stone" not in tokens._split_type_spellings()
    assert {"soap stone", "sand stone", "lime stone"} <= set(tokens._split_type_spellings())


def test_committed_base_names_are_untouched_by_form_and_spelling_rules():
    # The committed base is a fixed point; no form word or split spelling may alter ANY of its names, or the
    # seed stops being a fixed point / re-types a variety. This is the collision gate as a permanent guard.
    import csv
    from stone_pipeline.config.settings import SETTINGS
    with open(SETTINGS.paths.variants_export_base_csv, encoding="utf-8-sig", newline="") as h:
        names = [r["Name"] for r in csv.DictReader(h)]
    changed = [n for n in names if tokens._canonical_type_spelling(strip_forms(n))[0] != n]
    assert not changed, changed[:10]


# --- pack gating ---------------------------------------------------------------------------------------------

def test_a_pack_without_form_words_strips_nothing(monkeypatch):
    bare = dataclasses.replace(active_pack(), product_form_words=frozenset())
    monkeypatch.setattr(tokens, "active_pack", lambda: bare)
    tokens._form_patterns.cache_clear()
    try:
        assert strip_forms("Oak Step") == "Oak Step"
        assert clean_variety("Pietra Grey Marble Step", "Marble") == "Pietra Grey Step"
    finally:
        tokens._form_patterns.cache_clear()
