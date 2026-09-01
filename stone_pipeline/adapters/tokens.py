"""Light token extraction helpers for adapters (section 7 Stage 1).

Some sources carry only a generic descriptor name (marenostone "Cream Marble Tile") and the adapter must
strip known colour/type/format tokens to find the candidate variety. Others carry no colour column at all
(zucchi, varsha) but name the colour inside the stone name -- English ("Alaska Gold") or the source's own
language ("Granito Preto ..."). A light scan recovers it as a raw_ input that Stage 3 then resolves.

The recognition vocabulary is the SAME single source the Stage-3 resolver uses: the live Medusa export
(`attributes.csv`) for canonical values, plus `reference/synonyms/<vocab>.csv` for spellings. It is loaded
once and cached, keyed on the one shared normalizer (`core.text.match_key`). There is no hardcoded copy to
drift from Medusa.
"""

from __future__ import annotations

import html
import re
from functools import lru_cache

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import CATEGORIES
from stone_pipeline.core.text import looks_codey, match_key


@lru_cache(maxsize=None)
def known_values(vocab: str) -> tuple[str, ...]:
    """The canonical vocabulary for `vocab` ('color', 'type', ...), sourced live from the Medusa attribute
    export (`attributes.csv`) -- the single source shared with the resolver, so recognition can never drift
    from what the operator has in Medusa. Cached (loaded once per process). A missing export raises (from
    `load_attributes`): a real deploy error surfaced loudly, never masked behind a stale hardcoded list."""
    from stone_pipeline.reference.loaders import load_attributes
    return tuple(load_attributes().canonical_names(vocab))


# Type words that double as common variety-NAME descriptors ('Spectra Crystal' is a quartzite; 'Crystal'
# is a descriptor, not the type) now live in the active product-domain pack (pack.ambiguous_type_words);
# the stone pack reproduces the historical set.
# Negation prefixes that CANCEL a following type word -- 'Falsa Agata' (false agate) is genuinely an onyx,
# so the type word must not be read as the variety's type.
_NEGATION_WORDS = {"falsa", "false", "falso", "fake", "faux", "imitation"}


@lru_cache(maxsize=None)
def _clear_type_words() -> frozenset[str]:
    """Single-word stone-type words that UNAMBIGUOUSLY name a type in a variety name, match_key-keyed:
    every canonical type plus every type synonym, minus the descriptor-prone words. Multi-word types
    (e.g. 'Sodalite Syenite') are excluded -- a name carries the short synonym ('Sodalite'), not the pair."""
    from stone_pipeline.reference.loaders import load_synonyms
    words = {match_key(v) for v in known_values("type")} | set(load_synonyms("type"))
    return frozenset(w for w in words if " " not in w) - active_pack().ambiguous_type_words


def explicit_type_word(name: str) -> str | None:
    """The stone-TYPE word a variety name carries, IF it is an unambiguous rock type. A TRAILING type
    word is the common '{Variety} {Type}' convention ('Azul White Quartzite' -> 'Quartzite'); a LEADING
    type word covers '{Type} {Variety}' ('Sodalite Baia' -> 'Sodalite', 'Onyx Blue' -> 'Onyx'). None for:
    a single-token name (a variety NAMED after a type, e.g. 'Agate'), a descriptor-prone type word
    ('Spectra Crystal'), or no clear type word at either end."""
    toks = (name or "").split()
    if len(toks) < 2:
        return None
    clear = _clear_type_words()
    if match_key(toks[-1]) in clear:
        if match_key(toks[-2]) in _NEGATION_WORDS:          # 'Falsa Agata' -> not the Agate type
            return None
        return toks[-1]
    if match_key(toks[0]) in clear:
        return toks[0]
    return None


@lru_cache(maxsize=None)
def _type_lookup() -> dict[str, str]:
    """match_key(value) -> canonical stone type, from the live canonical types plus the type synonyms.
    The recognition vocabulary for reading a type from an EXPLICIT source field (a type/composition
    column), sharing match_key with the resolver. Unlike explicit_type_word (which scans a variety NAME
    and guards descriptor-prone words), an explicit type field IS a type assertion, so no word is guarded
    out here. Synonym keys are already match_key-normalized by the loader."""
    from stone_pipeline.reference.loaders import load_synonyms
    lookup = {match_key(canon): canon for canon in known_values("type")}
    for raw_key, canonical in load_synonyms("type").items():
        lookup.setdefault(raw_key, canonical)
    return lookup


def recognize_type(value: str) -> str:
    """The canonical Medusa stone type named by an EXPLICIT source field value, or '' if it names none.
    'Granite'/'granito' -> 'Granite', a classification tag ('Exotic', 'Stock Clearance') -> ''. For a
    MIXED source field (varsha's `composition`) that is sometimes a real type and sometimes a tag; the
    caller isolates any source-specific parsing (e.g. splitting on '/') and passes the candidate head."""
    return _type_lookup().get(match_key(value), "")


# plural + singular title of every category (registry), plural first so e.g. "Slabs" is stripped before
# "Slab". A scraper-side concept (the upload branch), not a Medusa attribute vocabulary.
FORMAT_TOKENS = [t for c in CATEGORIES for t in (c.label, c.name.title())]


def _word_re(token: str) -> re.Pattern:
    return re.compile(rf"\b{re.escape(token)}\b", flags=re.IGNORECASE)


@lru_cache(maxsize=None)
def _colour_lookup() -> dict[str, str]:
    """match_key(word) -> canonical colour, from the live canonical colours plus the colour synonyms. The
    one recognition vocabulary for colour; it shares `match_key` with the resolver, so the extractor
    recognises exactly what Stage 3 resolves. Synonym keys are already match_key-normalized by the loader."""
    from stone_pipeline.reference.loaders import load_synonyms
    lookup = {match_key(canon): canon for canon in known_values("color")}
    for raw_key, canonical in load_synonyms("color").items():
        lookup.setdefault(raw_key, canonical)
    return lookup


def _match_colour(text: str, lookup: dict[str, str]) -> str:
    """The FIRST colour word by TEXT POSITION whose normalized form is a known colour, returned canonical.
    Position-aware so a structured name '{type} {colour} {trade}' picks the colour, not a colour word that
    happens to sit in a trade name ('... Cinza Blue Sky' -> Grey, not Blue). Pure: the vocabulary is
    injected, so the logic is unit-testable without any file I/O."""
    for word in match_key(text).split():
        canonical = lookup.get(word)
        if canonical:
            return canonical
    return ""


def extract_color(text: str) -> str:
    """The colour named in `text`, recognised against the live colour vocabulary + synonyms (see
    `_match_colour`). Empty when the text names no known colour."""
    return _match_colour(text, _colour_lookup())


@lru_cache(maxsize=None)
def _strip_tokens() -> tuple[str, ...]:
    """Every colour/type word to strip from a descriptor name -- canonical values AND their synonyms
    (so 'Gray'/'Preto' strip like 'Grey'/'Black'), plus format words. One vocabulary, no hardcoding."""
    from stone_pipeline.reference.loaders import load_synonyms
    return (*known_values("color"), *load_synonyms("color"),
            *known_values("type"), *load_synonyms("type"), *FORMAT_TOKENS)


def strip_variety(name: str) -> str:
    """Remove colour, type, and format tokens from a descriptor name, leaving the candidate variety
    (often empty for a purely generic descriptor). Word-boundary matched, so 'Gold' never bites 'Golden'."""
    text = name or ""
    for token in _strip_tokens():
        text = _word_re(token).sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def strip_format(name: str) -> str:
    """Remove only the format word, keeping colour and type. The result is matched against the tree: a
    descriptor that is actually a real variety (White Travertine, Pink Onyx) then resolves, while a purely
    generic one (Cream Marble) finds no exact match and routes to review or gap rather than being discarded
    before it is even tried."""
    text = name or ""
    for token in FORMAT_TOKENS:
        text = _word_re(token).sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


# Format words as a casefold-comparable set (category name + plural), for clean_variety below.
_VARIETY_FORMAT_WORDS = {w for c in CATEGORIES for w in (c.name, c.plural)}


def clean_variety(name: str, stone_type: str) -> str:
    """The variety IDENTITY name for `name` given its resolved `stone_type` -- the ONE derivation shared by
    the matcher (to look up an operator mint decision for a product) and curate (to mint under). Suppliers
    name products '{Variety} {Type} {Format}' (e.g. 'Crystal White Granite Slab'). Always strip the format
    word (slab/block/tile). Strip the stone-type token(s) too, but ONLY when a distinctive multi-word name
    remains -- otherwise keep the type so we don't reduce a name to a bare generic colour ('Crystal White
    Granite Slab' -> 'Crystal White', but 'White Onyx Slab' -> 'White Onyx', not 'White'). Never empty."""
    toks = html.unescape(name or "").split()   # decode &#8211; etc. before cleaning
    type_toks = {t.casefold() for t in (stone_type or "").split()}
    no_fmt = [t for t in toks if t.casefold() not in _VARIETY_FORMAT_WORDS]
    no_type = [t for t in no_fmt if t.casefold() not in type_toks]
    # Strip the type when a distinctive multi-word name remains, OR when the single remaining token is a
    # supplier CODE -- so 'MGT Onyx' -> 'MGT' is caught downstream as a bare code and routed to review, NOT
    # minted as a type-baked variety 'Mgt Onyx'. Keep the type only when the remainder is a real word.
    chosen = no_type if (len(no_type) >= 2 or (len(no_type) == 1 and looks_codey(no_type[0]))) else no_fmt
    # A trailing bare <=2-char token is almost always a supplier lot/region code, not part of the variety
    # name ('White Super ES', ES = Espirito Santo). Drop it so the variety folds to its base name and the
    # full scraped name is kept as an alias. Only when >=2 real tokens remain, so never a bare colour/type.
    if len(chosen) >= 3 and len(chosen[-1]) <= 2 and chosen[-1].isalnum():
        chosen = chosen[:-1]
    return " ".join(chosen) or " ".join(no_fmt) or (name or "").strip()
