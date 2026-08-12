"""Deterministic name-identity resolver: an alias is decided on NAME evidence only (a near-identical
spelling, or a difference that is only generic descriptor words). Type and colour never create an alias --
that is what merged unrelated same-type/same-colour stones. No name evidence -> a new variety (mint)."""

from __future__ import annotations

from stone_pipeline.matching.alias_resolver import AliasResolver


def _v(a, ta, b, tb):
    # colours are passed but deliberately ignored by identity; a/ta is the scrape, b/tb the candidate.
    return AliasResolver().decide_against(a, ta, ["x"], [b], tb, ["x"]).verdict


def test_type_and_colour_alone_never_alias_distinct_stones():
    # the mis-bind bug: same type + same colour but a different NAME must MINT, never auto-alias.
    assert _v("Azul Palomino", "Quartzite", "Capolavoro", "Quartzite") == "mint"
    assert _v("Aphrodite", "Granite", "Blue Labradorite", "Granite") == "mint"
    assert _v("Amarillo Valencia", "Marble", "Crema Valencia", "Marble") == "mint"


def test_near_identical_spelling_is_an_alias():
    # a typo / spacing variant of essentially the same name -> alias (the typo net), even across a token
    # boundary ('Ice Burg' vs 'Iceberg' share no whole token but the de-spaced chars match).
    assert _v("Ice Burg", "Marble", "Iceberg", "Marble") == "alias"
    assert _v("Bianco Carara", "Marble", "Bianco Carrara", "Marble") == "alias"


def test_generic_descriptor_difference_is_an_alias():
    # a shared varietal core where the ONLY difference is generic descriptor words -> same variety.
    assert _v("Bardiglio Nuvolato Marble", "Marble", "Bardiglio Nuvolato", "Marble") == "alias"
    assert _v("Calacatta Gold Extra", "Marble", "Calacatta Gold", "Marble") == "alias"


def test_meaningful_differing_word_is_a_distinct_sibling():
    # a shared core with a MEANINGFUL differing word is a distinct variety, never an alias.
    assert _v("Cristallo Divine", "Quartzite", "Cristallo Bianco", "Quartzite") == "mint"
    assert _v("Calacatta Gold", "Marble", "Calacatta Viola", "Marble") == "mint"


def test_cross_type_same_name_never_auto_aliases():
    # a name shared across two stone types is TWO varieties; the confident match is downgraded to review,
    # never auto-merged (a Marble 'Tiger Black' and a Granite 'Tiger Black' are different stones).
    assert _v("Tiger Black", "Marble", "Tiger Black", "Granite") == "review"


def test_decide_against_scores_alias_surfaces_not_just_canonical():
    # the decision scores against a variety's aliases too, so a scrape matching an alias resolves even when
    # the canonical name is far.
    d = AliasResolver().decide_against("Stone AA Marble", "Marble", ["W"],
                                       ["Totally Different", "Stone AA"], "Marble", ["W"])
    assert d.verdict == "alias"


def test_generic_descriptor_detected_for_misselling_guard():
    # a name made only of colour + stone-type words is a generic trade name (its own variety, never aliased
    # up into a premium stone). A name with a varietal token is not generic. (Unchanged vocabulary check.)
    from stone_pipeline.matching import projections as proj
    from stone_pipeline.reference import loaders
    ref = loaders.load_all()
    words = set()
    for cat in ("color", "type"):
        for canon, _id in ref.attributes.by_category.get(cat, {}).values():
            words |= set(proj.norm(canon).split())

    def generic(n):
        t = set(proj.norm(n).split())
        return bool(t) and t <= words

    assert generic("Cream Quartzite") and generic("Black Granite") and generic("White Marble")
    assert not generic("Taj Mahal") and not generic("Macaubas Creme") and not generic("Cristallo Divine")
