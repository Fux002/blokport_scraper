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

# Type words that double as common variety-NAME descriptors -- never used to override a supplier's
# type ('Spectra Crystal' is a quartzite; 'Crystal' is a descriptor, not the type).
_AMBIGUOUS_TYPE_WORDS = {"crystal", "quartz", "agate", "amethyst", "coral"}
_CLEAR_TYPE_WORDS = {t.casefold() for t in TYPE_TOKENS if " " not in t} - _AMBIGUOUS_TYPE_WORDS


def explicit_type_word(name: str) -> str | None:
    """The stone-TYPE word a variety name explicitly ends with (suppliers name '{Variety} {Type}'),
    IF it is an unambiguous rock type. 'Azul White Quartzite' -> 'Quartzite', 'Grey Basalt' ->
    'Basalt'. None for: a single-token name (a variety NAMED after a type, e.g. 'Agate'), a
    descriptor-prone type word ('Spectra Crystal'), or no trailing type word ('Crystal White')."""
    toks = (name or "").split()
    if len(toks) < 2:
        return None
    return toks[-1] if toks[-1].casefold() in _CLEAR_TYPE_WORDS else None


# plural + singular title of every category (registry), plural first so e.g.
# "Slabs" is stripped before "Slab"
FORMAT_TOKENS = [t for c in CATEGORIES for t in (c.label, c.name.title())]


def _word_re(token: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(token)}\b", flags=re.IGNORECASE)


def extract_color(text: str) -> str:
    """First backend colour word found in the text, canonicalized (Gray -> Grey)."""
    if not text:
        return ""
    for token in COLOR_TOKENS:
        if _word_re(token).search(text):
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
