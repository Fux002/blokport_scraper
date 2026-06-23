"""Tier-7 alias resolver: a trained model tells a real alias (base + generic descriptor) from a
distinct sibling variety (base + a meaningful word), instead of a flat fuzzy threshold. The
behavioural assertions train on the real backbone via curate._alias_model (the production path)."""

from __future__ import annotations

from stone_pipeline.matching import projections as proj
from stone_pipeline.matching.alias_resolver import AliasResolver


def test_train_returns_none_without_enough_labels():
    assert AliasResolver.train([("Carrara", [], "Marble", ["White"])]) is None


def test_resolver_separates_siblings_from_generic_aliases():
    from stone_pipeline.stages.curate import _alias_model
    r, meta = _alias_model()
    assert r is not None
    assert r.accuracy >= 0.90                      # held-out, within the confident band

    def prob(a, ta, ca, b):
        nt, nc, _ = meta.get(proj.norm(b), ("", [], []))
        return r.prob(a, ta, ca, b, nt, nc)

    # distinct sibling varieties (shared family prefix, each with a meaningful word) -> mint
    assert prob("Cristallo Divine", "Quartzite", ["White"], "Cristallo Bianco") < 0.5
    # base + generic descriptor words (the real alias pattern) -> alias
    assert prob("Bardiglio Nuvolato Marble", "Marble", ["Grey"], "Bardiglio Nuvolato") > 0.5


def test_decide_against_uses_aliases_not_just_canonical_name():
    # the decision must score against a variety's aliases too, so a scrape matching an alias is an
    # alias even when the canonical name is far. Use a synthetic variety whose alias is the match.
    r = AliasResolver.train(
        [(f"Stone {chr(65 + i)}{chr(65 + j)}",
          [f"Stone {chr(65 + i)}{chr(65 + j)} Marble", f"Italian Stone {chr(65 + i)}{chr(65 + j)}"],
          "Marble", ["White"]) for i in range(8) for j in range(8)],
        hi=0.8, lo=0.2)
    assert r is not None
    near = r.decide_against("Stone AA Marble", "Marble", ["White"],
                            ["Totally Different", "Stone AA"], "Marble", ["White"])
    assert near.verdict == "alias"                 # matched via the alias surface, not the first one


def test_generic_descriptor_detected_for_misselling_guard():
    # a name made only of colour + stone-type words is a generic trade name (its own variety,
    # never aliased up into a premium stone). A name with a varietal token is not generic.
    from stone_pipeline.reference import loaders
    from stone_pipeline.matching import projections as proj
    ref = loaders.load_all()
    words = set()
    for cat in ("color", "type"):
        for canon, _id in ref.attributes.by_category.get(cat, {}).values():
            words |= set(proj.norm(canon).split())
    def generic(n):
        t = set(proj.norm(n).split()); return bool(t) and t <= words
    assert generic("Cream Quartzite") and generic("Black Granite") and generic("White Marble")
    assert not generic("Taj Mahal") and not generic("Macaubas Creme") and not generic("Cristallo Divine")
