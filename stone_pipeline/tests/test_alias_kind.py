"""The `alias` field kind (34's OWNED-NAMES-CLEAN row, the owner's ruling 13 Nov): field-kinds.json marks
variation_alias.value as kind "alias", and an owned alias is written by clean_alias, which KEEPS accents and every
script (never folded, never dropped) and only removes what the database class ^[[:alpha:]\u0300-\u036f0-9 '-]+$
refuses. The ONE refusal is mojibake (Mas, 13 Nov): no length bound; a sentence or a concatenation is fixed at its
source. Names keep kind "name". Matching and Keys are unchanged: match_key still folds for matching, and no alias ever
keys.

Red form: names the fix adds are read with a default, so each case fails on its assert. No em dashes."""
from __future__ import annotations

import json

import pytest

from stone_pipeline.core import text
from stone_pipeline.reference.loaders import field_kinds

ALIAS_KIND = getattr(field_kinds, "ALIAS", "alias")
_CLEAN_ALIAS = getattr(text, "clean_alias", None)
_REFUSED = getattr(text, "AliasRefused", None)

# 34's spec vectors (the shared name-key vectors gain them as `alias` cases; this table pins them meanwhile)
SPEC_VECTORS = [
    ("Giallo D`istria", "Giallo D'istria"),      # a backtick is an apostrophe (the typographic table)
    ("Huángjiā Hǔpò", "Huángjiā Hǔpò"),          # accents KEPT
    ("巴比伦金", "巴比伦金"),                        # a non-Latin script KEPT
    ("St. Laurent", "St Laurent"),               # a full stop is a space
    ("Celestial Splendor​", "Celestial Splendor"),   # an invisible U+200B goes (fd's dev count)
    ("Bian​co", "Bianco"),                         # format characters go FIRST: removed, never a space
    ("Who? Me", "Who Me"),                       # a '?' beside one letter only is not mojibake: a space
    ("Kırşehir's", "Kırşehir's"),                # an apostrophe between two letters of any script is kept
    ("Rosa  Porriño", "Rosa Porriño"),     # NFC composes a decomposed letter; spacing collapses
    ("Black & Gold", "Black and Gold"),          # '&' reads 'and'
    ("Verde – Lapponia", "Verde-Lapponia"),  # a dash is a hyphen; hyphen runs collapse
    ("'Nero' Marquina", "Nero Marquina"),        # an apostrophe only inside a word
    ("Bianco (Carrara) #1/2", "Bianco Carrara 1 2"),   # anything else is a space
    ("   ", ""),                                 # nothing left: refused by the caller, never stored
]


def _alias_kinds(monkeypatch):
    """The published map with variation_alias.value as kind `alias` (every other field as the fixture has it)."""
    published = dict(field_kinds.kinds())
    published[field_kinds.VARIATION_ALIAS_VALUE] = ALIAS_KIND
    monkeypatch.setattr(field_kinds, "kinds", lambda: published)


@pytest.mark.parametrize("raw,expected", SPEC_VECTORS)
def test_clean_alias_follows_the_spec(raw, expected):
    assert _CLEAN_ALIAS is not None and _CLEAN_ALIAS(raw) == expected


@pytest.mark.parametrize("raw", ["K?r?ehir Nero Picasso", "C?te D", "C\ufffdte"])
def test_mojibake_is_refused(raw):
    # a lost letter ('?' or U+FFFD with a letter on both sides): never stored, listed for the owner
    assert _CLEAN_ALIAS is not None and _REFUSED is not None
    with pytest.raises(_REFUSED):
        _CLEAN_ALIAS(raw)


def test_no_length_bound_refuses_a_long_alias():
    # Mas: mojibake is the only refusal; a long real name is kept, a sentence is cleaned (its fix is at the source)
    assert _CLEAN_ALIAS is not None
    assert _CLEAN_ALIAS("Bianco Statuarietto Venato Extra Calacatta Oro Gold") == (
        "Bianco Statuarietto Venato Extra Calacatta Oro Gold")
    assert _CLEAN_ALIAS("In Russian:nigrozero") == "In Russian nigrozero"


def test_the_reader_accepts_the_alias_kind():
    doc = {"brand": field_kinds.BRAND, "fields": {f: "name" for f in field_kinds.REQUIRED}}
    doc["fields"][field_kinds.VARIATION_ALIAS_VALUE] = "alias"
    try:
        fields = field_kinds.parse(json.dumps(doc).encode("utf-8"), "test")
    except field_kinds.FieldKindsError as exc:
        pytest.fail(f"the reader refused the alias kind: {exc}")
    assert fields[field_kinds.VARIATION_ALIAS_VALUE] == "alias"


def test_a_kind_outside_name_identifier_alias_is_still_refused():
    doc = {"brand": field_kinds.BRAND, "fields": {f: "name" for f in field_kinds.REQUIRED}}
    doc["fields"][field_kinds.VARIATION_ALIAS_VALUE] = "prose"
    with pytest.raises(field_kinds.FieldKindsError, match="prose"):
        field_kinds.parse(json.dumps(doc).encode("utf-8"), "test")


def test_display_and_clean_by_the_alias_kind_keep_accents_and_scripts(monkeypatch):
    _alias_kinds(monkeypatch)
    for raw, expected in (("Huángjiā Hǔpò", "Huángjiā Hǔpò"), ("巴比伦金", "巴比伦金"),
                          ("giallo d`istria", "Giallo D'istria")):
        try:
            shown = field_kinds.display(field_kinds.VARIATION_ALIAS_VALUE, raw)
        except text.UnfoldableName as exc:
            pytest.fail(f"an alias was refused for having no ASCII form: {exc}")
        assert shown == expected
    assert field_kinds.clean(field_kinds.VARIATION_ALIAS_VALUE, "St. Laurent") == "St Laurent"


def test_names_keep_the_name_kind(monkeypatch):
    _alias_kinds(monkeypatch)
    assert field_kinds.display(field_kinds.VARIATION_NAME, "St. Laurent") == "St Laurent"
    with pytest.raises(text.UnfoldableName):
        field_kinds.display(field_kinds.VARIATION_NAME, "巴比伦金")


def test_the_alias_list_keeps_every_script_and_drops_nothing(monkeypatch):
    _alias_kinds(monkeypatch)
    alias = lambda v: field_kinds.display(field_kinds.VARIATION_ALIAS_VALUE, v)   # noqa: E731
    kept, dropped = text.clean_alias_list(
        "Amber Gold", ["巴比伦金", "Huángjiā Hǔpò", "Huangjia Hupo", "Amber  Gold", "Kırşehir Nero", "K?r?ehir Nero"],
        alias)
    # the accented alias and its folded twin are ONE (match_key de-dup, first seen kept); the variety's own name goes;
    # only the mojibake is dropped, and it is listed
    assert kept == ["巴比伦金", "Huángjiā Hǔpò", "Kırşehir Nero"] and dropped == ["K?r?ehir Nero"]


def test_under_the_name_kind_an_unfoldable_alias_is_still_dropped(monkeypatch):
    # a map saying `name` for the alias field: the name rule, the alias dropped and listed
    published = dict(field_kinds.kinds())
    published[field_kinds.VARIATION_ALIAS_VALUE] = field_kinds.NAME
    monkeypatch.setattr(field_kinds, "kinds", lambda: published)
    alias = lambda v: field_kinds.display(field_kinds.VARIATION_ALIAS_VALUE, v)   # noqa: E731
    kept, dropped = text.clean_alias_list("Amber Gold", ["巴比伦金", "Huángjiā Hǔpò"], alias)
    assert kept == ["Huangjia Hupo"] and dropped == ["巴比伦金"]


def test_emit_writes_owned_aliases_by_the_alias_kind_and_keeps_key_and_name(monkeypatch):
    from stone_pipeline.stages import emit_catalog
    _alias_kinds(monkeypatch)
    row = {"Key": "granite__amber_gold_x", "Name": "Amber Gold", "Type": "Granite",
           "Aliases": "巴比伦金|Huángjiā Hǔpò|Celestial Splendor​|St. Laurent"}
    dropped = emit_catalog._write_names(row)
    assert dropped == []
    assert row["Aliases"].split("|") == ["巴比伦金", "Huángjiā Hǔpò", "Celestial Splendor", "St Laurent"]
    assert (row["Key"], row["Name"]) == ("granite__amber_gold_x", "Amber Gold")


def test_matching_keys_are_unchanged_by_the_alias_kind():
    # match_key still folds (matching never sees the display): an accented alias keys like its folded twin
    assert text.match_key("Huángjiā Hǔpò") == text.match_key("Huangjia Hupo") == "huangjia hupo"


# --- the paired alias key (Mas, 13 Nov; `alias_key` in the shared name-key vectors) -----------------------------------
_ALIAS_KEY = getattr(text, "alias_key", None)


def test_an_all_latin_alias_keys_as_matching_does():
    # Latin with diacritics stays on match_key, so no stored all-Latin key changes
    assert _ALIAS_KEY is not None
    for value in ("Huángjiā Hǔpò", "St. Laurent", "Black & Gold", "Kırşehir Nero"):
        assert _ALIAS_KEY(value) == text.match_key(value)


def test_a_non_latin_alias_keeps_its_script_and_never_collides_with_its_latin_part():
    assert _ALIAS_KEY is not None
    assert _ALIAS_KEY("巴比伦金") == "巴比伦金"
    assert _ALIAS_KEY("3D灰") == "3d灰" != _ALIAS_KEY("3D") == "3d"
    assert _ALIAS_KEY("Wratza 维纳多") == "wratza 维纳多" != _ALIAS_KEY("Wratza")
    assert _ALIAS_KEY("ΜΑΡΜΑΡΟΣ") == "μαρμαρος"                 # lower(), not casefold: the final sigma stays
    assert _ALIAS_KEY("\u2014") == ""                              # nothing left: the callers refuse it


def test_the_alias_check_keys_a_non_latin_value_by_alias_key():
    # a pure-CJK value is a value (match_key reads it empty); a mixed one is not its Latin part's own name
    from stone_pipeline.core.alias_problem import AliasContext, alias_problem
    context = AliasContext(own_name="Wratza", type_words=frozenset({"granite"}), vocabulary_keys=frozenset(),
                           same_type_name_keys=frozenset())
    assert alias_problem("巴比伦金", context) is None
    assert alias_problem("Wratza 维纳多", context) is None


def _wratza():
    from stone_pipeline.core.alias_problem import AliasContext
    return AliasContext(own_name="Wratza", type_words=frozenset({"granite"}), vocabulary_keys=frozenset(),
                        same_type_name_keys=frozenset())


def test_too_short_applies_to_the_latin_key_only():
    # 34, 13 Nov: a CJK character carries a word, so a script-preserving key is never too short
    from stone_pipeline.core.alias_problem import alias_problem
    assert alias_problem("灰石", _wratza()) is None
    assert alias_problem("3D灰", _wratza()) is None
    assert alias_problem("ab", _wratza()) == "too_short"


def test_a_code_is_judged_on_every_token_of_its_alias_key():
    # 34, 13 Nov: codeMatcher tokenises by alias_key, so a non-Latin token never hides inside a code
    from stone_pipeline.core.alias_problem import code_matcher
    is_code = code_matcher(r"[Gg]\d+[A-Za-z]?")
    assert is_code("G12") and is_code("g654 G12a")
    assert not is_code("G12 灰")
