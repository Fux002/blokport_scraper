"""The indefinite article a product description puts before a finish, a material or a category word: "an" before a
vowel SOUND, "a" before a consonant sound, read from the first word. The spec pins every finish, type and colour of the
attribute vocabulary and every category word, so a new value whose article is not the letter rule's (a "Uniform"
finish, an "Hour" anything) fails here before it reaches a buyer."""

from __future__ import annotations

import pytest

from stone_pipeline.config.categories import CATEGORIES
from stone_pipeline.core.text import indefinite_article
from stone_pipeline.reference.loaders import load_attributes

# The vocabulary values that take "an": every one starts with a vowel sound. Every other value takes "a".
AN = {
    "finish": {"Acid Washed", "Antique", "Oiled", "Unfilled"},
    "type": {"Agate", "Alabaster", "Amethyst", "Andesite", "Onyx"},
    "color": {"Ivory", "Orange", "Orchid"},
}
# the vocabulary this spec was written against; types are the PACK's (table 569): dev's 31 less Dolomite Marble, which
# has no combination and no seed variety (it takes "a", so AN is unchanged)
COUNTS = {"finish": 51, "type": 30, "color": 25}


@pytest.mark.parametrize("category", sorted(AN))
def test_every_vocabulary_value_takes_its_article(category):
    values = load_attributes().canonical_names(category)
    assert len(set(values)) == COUNTS[category]
    assert {v for v in values if indefinite_article(v) == "an"} == AN[category]
    assert all(indefinite_article(v.lower()) == indefinite_article(v) for v in values)    # the case never matters


def test_every_category_word_and_the_unresolved_word_take_a():
    assert {indefinite_article(c.name) for c in CATEGORIES} == {"a"}
    assert indefinite_article("piece") == "a"


@pytest.mark.parametrize("phrase, article", [
    ("uniform slab", "a"), ("unique block", "a"), ("European oak", "a"), ("used slab", "a"), ("UV-cured tile", "a"),
    ("one-off slab", "a"), ("one", "a"),
    ("hour", "an"), ("honest finish", "an"), ("honour", "an"), ("heir", "an"), ("hourly", "an"),
    ("Honed slab", "a"), ("Unfilled slab", "an"), ("Oiled plank", "an"), ("unpolished slab", "an"),
    ("onerous", "an"), ("  antique slab", "an"),
])
def test_the_sound_exceptions(phrase, article):
    assert indefinite_article(phrase) == article


@pytest.mark.parametrize("phrase", ["", "   "])
def test_no_word_has_no_article(phrase):
    with pytest.raises(ValueError):
        indefinite_article(phrase)
