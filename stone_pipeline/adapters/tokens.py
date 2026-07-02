"""Light token extraction helpers for adapters (section 7 Stage 1).

Some sources carry only a generic descriptor name (marenostone "Cream Marble
Tile") and the adapter must strip known colour, type, and format tokens to find
the candidate variety, leaving it empty when nothing remains. Others carry no
colour column at all (zucchi, varsha) but embed a colour word in the stone name
("Alaska Gold"); a light scan recovers it. This is light parsing, not
normalization: the recovered value is still a raw_ input that Stage 3 resolves.

The token lists mirror the backend vocabulary but are kept here as plain data so
adapters do not depend on a reference load at parse time.
"""

from __future__ import annotations

import re

from stone_pipeline.config.settings import CATEGORIES
from stone_pipeline.core.text import ascii_fold, match_key

COLOR_TOKENS = [
    "Beige", "Black", "Blue", "Bordeaux", "Bronze", "Brown", "Copper", "Cream",
    "Golden", "Gold", "Green", "Grey", "Gray", "Ivory", "Lilac", "Multicolor",
    "Orange", "Pink", "Purple", "Red", "Rose", "Silver", "White", "Yellow",
]

TYPE_TOKENS = [
    "Agate", "Alabaster", "Amethyst", "Andesite", "Basalt", "Bluestone", "Cantera",
    "Conglomerate", "Coral Stone", "Crystal", "Dolomite", "Gneiss", "Granite",
    "Limestone", "Marble", "Onyx", "Porphyry", "Quartzite", "Quartz", "Rhyolite",
    "Sandstone", "Schist", "Serpentine", "Slate", "Soapstone", "Travertine", "Tuff",
]

# Commercial / short synonyms mapping a name token to a canonical backend type. Suppliers and buyers
# say 'Sodalite'; the backend type is the geological 'Sodalite Syenite'. Registering the synonym lets
# both the name-over-tag correction recognise it AND resolve_id map it to the real attribute type.
TYPE_SYNONYMS = {
    "sodalite": "Sodalite Syenite",
    "sodalita": "Sodalite Syenite",        # Spanish/Portuguese spelling
    "blue sodalite": "Sodalite Syenite",
    "agata": "Agate",                      # Italian/Portuguese spelling of Agate (the stone, not a descriptor)
}

# Type words that double as common variety-NAME descriptors -- never used to override a supplier's
# type ('Spectra Crystal' is a quartzite; 'Crystal' is a descriptor, not the type).
_AMBIGUOUS_TYPE_WORDS = {"crystal", "quartz", "agate", "amethyst", "coral"}
# Negation prefixes that CANCEL a following type word -- 'Falsa Agata' (false agate) is genuinely an
# onyx, not the Agate type, so the type word must not be read as the variety's type.
_NEGATION_WORDS = {"falsa", "false", "falso", "fake", "faux", "imitation"}
# keyed by the shared match_key so an accented scrape ('Quartzíte') matches the vocab ('Quartzite')
_CLEAR_TYPE_WORDS = {match_key(w) for w in (*TYPE_TOKENS, *TYPE_SYNONYMS)
                     if " " not in w} - _AMBIGUOUS_TYPE_WORDS


def explicit_type_word(name: str) -> str | None:
    """The stone-TYPE word a variety name carries, IF it is an unambiguous rock type. A TRAILING type
    word is the common '{Variety} {Type}' convention ('Azul White Quartzite' -> 'Quartzite'); a
    LEADING type word covers '{Type} {Variety}' ('Sodalite Baia' -> 'Sodalite', 'Onyx Blue' ->
    'Onyx'). None for: a single-token name (a variety NAMED after a type, e.g. 'Agate'), a
    descriptor-prone type word ('Spectra Crystal'), or no clear type word at either end."""
    toks = (name or "").split()
    if len(toks) < 2:
        return None
    if match_key(toks[-1]) in _CLEAR_TYPE_WORDS:
        if match_key(toks[-2]) in _NEGATION_WORDS:          # 'Falsa Agata' -> not the Agate type
            return None
        return toks[-1]
    if match_key(toks[0]) in _CLEAR_TYPE_WORDS:
        return toks[0]
    return None


# plural + singular title of every category (registry), plural first so e.g.
# "Slabs" is stripped before "Slab"
FORMAT_TOKENS = [t for c in CATEGORIES for t in (c.label, c.name.title())]


def _word_re(token: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(token)}\b", flags=re.IGNORECASE)


def extract_color(text: str) -> str:
    """First backend colour word found in the text, canonicalized (Gray -> Grey)."""
    if not text:
        return ""
    folded = ascii_fold(text)   # match colour words regardless of surrounding accents
    for token in COLOR_TOKENS:
        if _word_re(token).search(folded):
            return "Grey" if token == "Gray" else token
    return ""


def strip_variety(name: str) -> str:
    """Remove colour, type, and format tokens from a descriptor name, leaving the
    candidate variety (often empty for a purely generic descriptor)."""
    text = name or ""
    for token in (*COLOR_TOKENS, *TYPE_TOKENS, *FORMAT_TOKENS):
        text = _word_re(token).sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_format(name: str) -> str:
    """Remove only the format word, keeping colour and type. The result is matched
    against the tree: a descriptor that is actually a real variety (White
    Travertine, Pink Onyx) then resolves, while a purely generic one (Cream
    Marble) finds no exact match and routes to review or gap rather than being
    discarded before it is even tried."""
    text = name or ""
    for token in FORMAT_TOKENS:
        text = _word_re(token).sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()
