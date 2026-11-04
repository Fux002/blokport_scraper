"""clean_name and clean_identifier: the two display rules (owner, 2026-11-03).

NAMES (variety, alias, type, colour, origin, port, category) are plain ASCII letters, digits, spaces, hyphens and
apostrophes: typographic punctuation is unified, invisible characters removed, accents and compatibility forms
folded, '&' reads 'and', and every other punctuation or symbol becomes a space. IDENTIFIERS (quality grades, finish
and standard codes, product titles) keep normal ASCII punctuation, because 'AA/BB' is an industry grade. In both, a
letter with no ASCII form (a non-Latin script) is refused with UnfoldableName and the caller decides, and the
cleaned value always keys like the original (match_key) ('&' reads 'and' in both, owner D1).
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.text import UnfoldableName, clean_identifier, clean_name, match_key


@pytest.mark.parametrize("raw, clean", [
    ("Rosa Porriño", "Rosa Porrino"),
    ("Rosa Porrin\u0303o", "Rosa Porrino"),          # decomposed accent
    ("Ma\u0331rmol Crema", "Marmol Crema"),           # a mark with no precomposed letter
    ("Ǣgean Blue", "AEgean Blue"),               # a transliteration letter carrying a mark
    ("Black & Gold", "Black and Gold"),
    ("St. Laurent", "St Laurent"),
    ("O’Brien", "O'Brien"),                      # curly and straight apostrophes give the SAME name
    ("O'Brien", "O'Brien"),
    ("Giallo D'oro", "Giallo D'oro"),
    ("Bianco Carrara C/D", "Bianco Carrara C D"),
    ("Semi–Precious", "Semi-Precious"),          # every dash is a hyphen
    ("Semi-Precious", "Semi-Precious"),
    ("Grey\u2060 Finish", "Grey Finish"),             # invisible format character
    ("Ras  Tannurah", "Ras Tannurah"),
    ("a\u00a0b", "a b"),
    ("Ｇ６０３", "G603"),              # fullwidth compatibility forms
    ("Nero (Spain)", "Nero Spain"),                    # brackets: the caller splits them into an alias FIRST
    ("-Lead-", "Lead"),
    ("...", ""),
    ("Absolute Black\u2122", "Absolute Black"),       # a symbol is a separator, never the letters 'TM'
    ("'Nero'", "Nero"),                                # an apostrophe is kept only inside a word
    ("O''Brien", "O'Brien"),
    ("Rock 'n' Roll", "Rock n Roll"),
    ("''", ""),
    ("Black\uff06Gold", "Black and Gold"),
    ("D\uff07Oro", "D'Oro"),
    ("Black\uf005Gold", "Black Gold"),                 # a private-use glyph (an icon font) separates
])
def test_clean_name(raw, clean):
    assert clean_name(raw) == clean


@pytest.mark.parametrize("raw, clean", [
    ("AA/BB", "AA/BB"),                                # an industry grade keeps its slash
    ("BB/BB (II/II)", "BB/BB (II/II)"),
    ("F17 Formply (AS6669)", "F17 Formply (AS6669)"),
    ("Combi (Parica + Eucalyptus)", "Combi (Parica + Eucalyptus)"),
    ("Film Dynea/StoraEnso 120g", "Film Dynea/StoraEnso 120g"),
    ("X & Squared", "X & Squared"),
    ("Pine Commercial Plywood / 18mm", "Pine Commercial Plywood / 18mm"),
    ("Grade\u00a0A–B", "Grade A-B"),             # an unusual space and an en dash
    ("“Premium”", '"Premium"'),             # typographic quotes become ASCII
    ("Ma\u0301rmol  Grade  A", "Marmol Grade A"),
    ("Film 120\u00b5m", "Film 120um"),                 # the micro sign is written 'u' in ASCII
    ("Silestone\u2122 Grade A", "Silestone Grade A"),
    ("AA\uff0fBB", "AA/BB"),                          # a fullwidth form of one ASCII mark is that mark
])
def test_clean_identifier(raw, clean):
    assert clean_identifier(raw) == clean


@pytest.mark.parametrize("clean", [clean_name, clean_identifier])
@pytest.mark.parametrize("raw", ["黑金砂", "Nero 黑 Marquina", "Серый",
                                 "أسود"])
def test_a_letter_with_no_ascii_form_is_refused(clean, raw):
    with pytest.raises(UnfoldableName):
        clean(raw)


@pytest.mark.parametrize("clean", [clean_name, clean_identifier])
def test_cleaning_is_a_fixed_point(clean):
    for raw in ["Rosa Porriño", "Black & Gold", "O’Brien", "St. Laurent", "Semi–Precious", "AA/BB"]:
        once = clean(raw)
        assert clean(once) == once


def _breaks(raw: str, clean) -> bool:
    """True when cleaning `raw` changes its identity or cleaning it again changes it further."""
    try:
        cleaned = clean(raw)
    except UnfoldableName:
        return False   # a refused value has no display to compare
    return match_key(cleaned) != match_key(raw) or clean(cleaned) != cleaned


def test_cleaning_keeps_identity_and_is_a_fixed_point_for_any_character():
    # Every BMP code point (controls, unassigned and private use included), inside a word and between spaces: the
    # cleaned value keys exactly like the original and is its own cleaned form ('&' reads 'and' in the key too,
    # owner D1).
    broken = []
    for cp in range(0x10000):
        ch = chr(cp)
        for raw in (f"Black{ch}Gold", f"Black {ch} Gold"):
            for clean in (clean_name, clean_identifier):
                if _breaks(raw, clean):
                    broken.append((clean.__name__, hex(cp), raw))
    assert not broken, f"{len(broken)} inputs change identity or are no fixed point, first: {broken[:5]}"


def test_format_characters_never_split_a_word_and_unusual_spaces_never_glue_two():
    assert match_key("Mar\u00admol") == match_key("Marmol")        # soft hyphen (Cf) is invisible
    assert match_key("Black\u2060Gold") == match_key("BlackGold")  # word joiner (Cf)
    assert match_key("Black\u200bGold") == "black gold"         # a zero-width space marks a word break
    for space in ("\u2028", "\u1680", "\u0085"):
        assert match_key(f"Black{space}Gold") == "black gold"
    # a control character is mojibake of a separator (U+0092 is cp1252's curly apostrophe read as Latin-1)
    for control in ("\u0092", "\u0096", "\x00", "\x1f"):
        assert match_key(f"O{control}Brien") == "o brien"
    assert clean_name("Black•Gold") == "Black Gold"           # a non-ASCII bullet is a separator
    assert clean_name("Black﹘Gold") == "Black-Gold"           # any dash, compatibility forms included
    assert clean_name("Oʻzbek") == "Ozbek"                    # a modifier letter is not refused
    assert clean_name("Black--Gold") == clean_name("Black - Gold") == "Black-Gold"


def test_a_symbol_is_a_separator_decided_on_the_original_character():
    # Decomposed first, a symbol would become letters and join the word: 'Absolute Black\u2122' keyed
    # 'absolute blacktm' and split the stone from 'Absolute Black'.
    assert match_key("Absolute Black\u2122") == "absolute black"
    assert match_key("\u2116 5") == "5"
    assert match_key("\u24b6bc") == "bc"
    assert match_key("\u00bd slab") == "1 2 slab"        # what decomposition yields ('1\u20442') is separated too
    assert clean_name("Para\u0140lel") == clean_name("Paral\u00b7lel") == "Paral lel"   # ela geminada
