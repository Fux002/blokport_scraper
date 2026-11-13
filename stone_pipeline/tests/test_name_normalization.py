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
    "Semi\u2014Precious/Stone",   # em dash + slash
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
    for a, b in [("Black & Gold", "Black and Gold"), ("St. Laurent", "St Laurent"),
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


def test_a_names_display_is_ascii_and_identity_still_folds():
    # DISPLAY of a name is its field's rule: cleaned to ASCII, then title-cased (owner O1). title_case itself is
    # casing only and decides no alphabet; IDENTITY folds, so the accented and plain spellings are ONE stone.
    from stone_pipeline.reference.loaders import field_kinds as fk
    assert fk.display(fk.VARIATION_NAME, "AZUL MACA\u00daBAS") == "Azul Macaubas"
    assert fk.display(fk.VARIATION_NAME, "quartzo vitro ros\u00e9") == "Quartzo Vitro Rose"
    assert fk.display(fk.VARIATION_NAME, "s\u00e3o GABRIEL") == "Sao Gabriel"
    assert match_key("Azul Maca\u00fabas") == match_key("Azul Macaubas")


def test_title_case_lowercases_connectors_except_leading():
    from stone_pipeline.core.text import title_case
    assert title_case("giallo DI siena") == "Giallo di Siena"        # connector lower, mid-name
    assert title_case("blanco Y negro") == "Blanco y Negro"
    assert title_case("de something") == "De Something"              # connector leading -> capitalized
    assert title_case("verde de fantasia") == "Verde de Fantasia"


def test_alias_dedup_is_accent_and_case_insensitive():
    # aliases differing only by accent/case collapse to one (alias_key), the first kept with its accent (the alias
    # kind, Mas 13 Nov)
    from functools import partial
    from stone_pipeline.core.text import clean_alias_list
    from stone_pipeline.reference.loaders import field_kinds as fk
    clean = partial(fk.display, fk.VARIATION_ALIAS_VALUE)
    assert clean_alias_list("Rosa", ["Ros\u00e9", "rose", "ROS\u00c9"], clean) == (["Ros\u00e9"], [])


def test_match_key_decomposed_accent_does_not_fork_identity():
    # Pages and PDFs made on macOS often send decomposed text (NFD): 'n' + U+0303 instead of 'ñ'.
    # A combining mark is not \w, so before the fix the punctuation pass turned it into a SPACE:
    # 'Porrin\u0303o' -> 'porrin o', a second key for the same stone.
    nfc, nfd = "Rosa Porriño", "Rosa Porrin\u0303o"
    assert match_key(nfd) == match_key(nfc) == "rosa porrino"
    assert match_key("Cre\u0300me Brule\u0301e") == match_key("Crème Brûlée") == "creme brulee"
    assert proj.norm(nfd) == proj.norm(nfc)


def test_match_key_mark_without_a_composed_form_does_not_fork_identity():
    # NFC composes only marks that HAVE a precomposed letter. U+0331 (combining macron below) has none
    # on most letters, so after NFC it is still a separate, non-\w mark: marks must be dropped before
    # the punctuation pass, not merely composed.
    assert match_key("Ma\u0331rmol Crema") == match_key("Marmol Crema") == "marmol crema"
    assert match_key("e\u0301\u0331te") == "ete"


def test_ascii_fold_keeps_a_transliteration_letter_that_carries_a_mark():
    # Ǣ and Ǿ decompose to Æ and Ø plus a mark. Transliterating BEFORE decomposing missed them, so the
    # letter was dropped as non-ASCII: the Key slug and proj.compact lost it while match_key kept it.
    from stone_pipeline.core.text import ascii_fold, slugify
    assert ascii_fold("Ǣgean") == "AEgean"
    assert ascii_fold("Ǿstfold") == "Ostfold"
    assert slugify("Ǣgean Blue") == slugify("Aegean Blue")
    assert proj.compact("Ǿ Grey") == proj.compact("O Grey")


def test_ampersand_reads_and_in_the_key_as_in_a_cleaned_name():
    # Owner D1 (2026-11-04): clean_name writes '&' as 'and', so the key reads it the same way; a scraped
    # 'Black & Gold' then still matches its own cleaned spelling 'Black and Gold'.
    assert match_key("Black & Gold") == match_key("Black and Gold") == "black and gold"
    assert match_key("Black\uff06Gold") == "black and gold"            # the fullwidth form too
    assert match_key("AT&T") == "at and t"


def test_compact_is_the_key_without_spaces():
    # One reading for every projection: compact agrees with the key on '&', symbols and marks.
    for a, b in [("Black & Gold", "Black and Gold"), ("Absolute Black\u2122", "Absolute Black"),
                 ("Rosa Porrin\u0303o", "Rosa Porrino"), ("Mont-Blanc", "MontBlanc")]:
        assert proj.compact(a) == proj.compact(b) == "".join(match_key(a).split())


def test_the_ascii_fast_path_is_exact():
    # match_key skips the shared preparation for ASCII text. That is only correct if the preparation never
    # changes an ASCII string's key: check every 1- and 2-character ASCII string.
    import itertools
    from stone_pipeline.core.text import _prepared
    chars = [chr(i) for i in range(128)]
    for n in (1, 2):
        for s in map("".join, itertools.product(chars, repeat=n)):
            assert match_key(s) == match_key(_prepared(s)), repr(s)


def test_one_slug_rule_for_handles_keys_and_identity():
    # A slug is the name's key words joined (W4), so a slug, a Key and the match key read a name one way: a curly
    # and a straight apostrophe, and '&' and 'and', give one Key and one handle.
    from stone_pipeline.core.text import slugify
    from stone_pipeline.reference.loaders.identity import variety_identity
    from stone_pipeline.phase1.variety_key import new_key
    assert new_key("Marble", "Valle D’Aosta") == new_key("Marble", "Valle D'Aosta")
    assert new_key("Marble", "Black & Gold") == new_key("Marble", "Black and Gold")
    assert slugify("Black & Gold Polished") == "black-and-gold-polished"
    assert slugify("Absolute Black™") == "absolute-black"
    key = "granite__x_1"
    assert variety_identity(key, "Valle D’Aosta") == variety_identity(key, "Valle D'Aosta")
