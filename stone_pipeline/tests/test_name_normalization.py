"""The canonical name-match key: a separator, case, or accent difference must NEVER fork the
identity of the same name anywhere in the pipeline.

Regression for the Semi-Precious Stone bug -- the stone type 'Semi-Precious Stone' (hyphen) did
not match the Key slug 'semi_precious_stone' (underscore/space), so 36 varieties stayed untyped
and their products never synced. The fix is one shared normalizer (core.text.match_key) that all
resolvers key on; the matching-engine projection (proj.norm) folds the same way.
"""

from __future__ import annotations

from stone_pipeline.core.text import match_key
from stone_pipeline.matching import projections as proj

# every spelling of the same name -- hyphen, underscore, spaces, unicode en/em dash, slash, tabs
_SAME = [
    "Semi-Precious Stone",
    "semi_precious_stone",
    "semi precious stone",
    "SEMI-PRECIOUS  STONE",
    "Semi–Precious Stone",   # en dash
    "Semi—Precious/Stone",   # em dash + slash
    "semi\tprecious\nstone",
]


def test_match_key_folds_separators_case_accents():
    assert len({match_key(v) for v in _SAME}) == 1, "a separator/case difference forked the name"
    assert match_key("Rosa Porriño") == match_key("Rosa Porrino"), "accent must fold"
    assert match_key("") == "" and match_key(None) == ""  # type: ignore[arg-type]


def test_proj_norm_folds_the_same_way():
    # the matching-engine key must not diverge: underscore (a \w char) and unicode dashes included
    assert len({proj.norm(v) for v in _SAME}) == 1, "proj.norm forked the name on a separator"


def test_match_key_folds_all_punctuation_to_agree_with_proj_norm():
    # match_key must fold ALL punctuation (not just separators), so it agrees with proj.norm and the
    # backbone/attribute vocab (match_key-keyed) joins the match index (proj.norm-keyed).
    for a, b in [("Black & Gold", "Black Gold"), ("St. Laurent", "St Laurent"),
                 ("Crema Marfil, Extra", "Crema Marfil Extra"), ("King's Blue", "King s Blue")]:
        assert match_key(a) == match_key(b) == proj.norm(a), f"{a!r} must fold like {b!r}"


def test_explicit_type_word_is_accent_insensitive():
    # a scraped type word carrying an accent must still be recognized as the stone type
    from stone_pipeline.adapters.tokens import explicit_type_word
    assert explicit_type_word("Azul Quartzíte") == "Quartzíte"   # accented trailing type word
    assert explicit_type_word("Sódalite Baia") == "Sódalite"     # accented leading type word


def test_country_and_variety_keys_route_through_match_key():
    # the whole system now shares one matcher: proj.norm (index), loaders._norm, match_key all equal
    from stone_pipeline.reference.loaders import _norm as loaders_norm
    for v in ["United-States", "United_States", "united  states"]:
        assert loaders_norm(v) == match_key(v) == proj.norm(v) == "united states"
