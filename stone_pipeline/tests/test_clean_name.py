"""clean_name and clean_identifier: the two display rules (owner, 2026-11-03).

NAMES (variety, alias, type, colour, origin, port, category) are plain ASCII letters, digits, spaces, hyphens and
apostrophes: typographic punctuation is unified, invisible characters removed, accents and compatibility forms
folded, '&' reads 'and', and every other mark becomes a space. IDENTIFIERS (quality grades, finish and standard
codes, product titles) keep normal ASCII punctuation, because 'AA/BB' is an industry grade. In both, a letter with
no ASCII form (a non-Latin script) is refused with UnfoldableName and the caller decides, and the cleaned value
always keys like the original (match_key), '&' in a name being the one deliberate change.
"""

from __future__ import annotations

import unicodedata

import pytest

from stone_pipeline.core.text import UnfoldableName, clean_identifier, clean_name, match_key


@pytest.mark.parametrize("raw, clean", [
    ("Rosa Porriño", "Rosa Porrino"),
    ("Rosa Porriño", "Rosa Porrino"),          # decomposed accent
    ("Ma̱rmol Crema", "Marmol Crema"),           # a mark with no precomposed letter
    ("Ǣgean Blue", "AEgean Blue"),               # a transliteration letter carrying a mark
    ("Black & Gold", "Black and Gold"),
    ("St. Laurent", "St Laurent"),
    ("O’Brien", "O'Brien"),                      # curly and straight apostrophes give the SAME name
    ("O'Brien", "O'Brien"),
    ("Giallo D'oro", "Giallo D'oro"),
    ("Bianco Carrara C/D", "Bianco Carrara C D"),
    ("Semi–Precious", "Semi-Precious"),          # every dash is a hyphen
    ("Semi-Precious", "Semi-Precious"),
    ("Grey⁠ Finish", "Grey Finish"),             # invisible format character
    ("Ras  Tannurah", "Ras Tannurah"),
    ("a b", "a b"),
    ("Ｇ６０３", "G603"),              # fullwidth compatibility forms
    ("Nero (Spain)", "Nero Spain"),                    # brackets: the caller splits them into an alias FIRST
    ("-Lead-", "Lead"),
    ("...", ""),
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
    ("Grade A–B", "Grade A-B"),             # an unusual space and an en dash
    ("“Premium”", '"Premium"'),             # typographic quotes become ASCII
    ("Mármol  Grade  A", "Marmol Grade A"),
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


def _identity_holds(raw: str, clean) -> bool:
    try:
        cleaned = clean(raw)
    except UnfoldableName:
        return True   # a refused value has no display to compare
    return match_key(cleaned) == match_key(raw)


def test_cleaning_never_changes_identity_for_any_character():
    # Every assigned BMP character, inside a word and between spaces: the cleaned value keys exactly like the
    # original. '&' (in any form) is the one deliberate change in a name (owner O1: it reads 'and').
    broken = []
    for cp in range(0x20, 0x10000):
        ch = chr(cp)
        if "&" in unicodedata.normalize("NFKD", ch) or unicodedata.category(ch) in ("Cn", "Cs", "Co"):
            continue
        for raw in (f"Black{ch}Gold", f"Black {ch} Gold"):
            for clean in (clean_name, clean_identifier):
                if not _identity_holds(raw, clean):
                    broken.append((clean.__name__, hex(cp), raw))
    assert not broken, f"{len(broken)} inputs change identity, first: {broken[:5]}"


def test_format_characters_never_split_a_word_and_unusual_spaces_never_glue_two():
    assert match_key("Mar­mol") == match_key("Marmol")        # soft hyphen (Cf) is invisible
    assert match_key("Black​Gold") == match_key("BlackGold")  # zero-width space (Cf)
    for space in (" ", " ", "\u0085"):
        assert match_key(f"Black{space}Gold") == "black gold"
    assert clean_name("Black•Gold") == "Black Gold"           # a non-ASCII bullet is a separator
    assert clean_name("Black﹘Gold") == "Black-Gold"           # any dash, compatibility forms included
    assert clean_name("Oʻzbek") == "Ozbek"                    # a modifier letter is not refused
    assert clean_name("Black--Gold") == clean_name("Black - Gold") == "Black-Gold"
