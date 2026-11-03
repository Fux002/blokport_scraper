"""clean_name: the ONE display-name rule (owner, 2026-11-03, report 21 decision O1).

A name is plain ASCII letters, digits, spaces and hyphens: typographic punctuation is unified first, invisible
format characters are removed, compatibility forms are folded, accents are folded, '&' reads 'and', and every
other mark becomes a space. A letter with no ASCII form (a non-Latin script) is never stored half-folded: the
name is refused with UnfoldableName and the caller decides (an alias is dropped, a variety goes to review).
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.text import UnfoldableName, clean_name, match_key


@pytest.mark.parametrize("raw, clean", [
    ("Rosa Porriño", "Rosa Porrino"),
    ("Rosa Porriño", "Rosa Porrino"),          # decomposed accent
    ("Ma̱rmol Crema", "Marmol Crema"),           # a mark with no precomposed letter
    ("Ǣgean Blue", "AEgean Blue"),               # a transliteration letter carrying a mark
    ("Black & Gold", "Black and Gold"),
    ("St. Laurent", "St Laurent"),
    ("O’Brien", "O Brien"),                       # curly and straight apostrophes give the SAME name
    ("O'Brien", "O Brien"),
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


@pytest.mark.parametrize("raw", ["黑金砂", "Nero 黑 Marquina", "Серый", "أسود"])
def test_a_letter_with_no_ascii_form_is_refused(raw):
    with pytest.raises(UnfoldableName):
        clean_name(raw)


def test_clean_name_is_a_fixed_point():
    for raw in ["Rosa Porriño", "Black & Gold", "O’Brien", "St. Laurent", "Semi–Precious"]:
        once = clean_name(raw)
        assert clean_name(once) == once


def test_punctuation_and_case_never_change_the_match_key_except_ampersand():
    # The display rule turns punctuation into spaces exactly as match_key does, so a cleaned name keeps its
    # identity. '&' is the one deliberate change (owner O1: it reads 'and').
    for raw in ["St. Laurent", "O’Brien", "Bianco Carrara C/D", "Rosa Porriño", "Semi–Precious"]:
        assert match_key(clean_name(raw)) == match_key(raw)
