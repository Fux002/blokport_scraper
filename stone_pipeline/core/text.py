"""Small deterministic text helpers for generation (section 10.3, 10.4)."""

from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")


# Latin letters that do NOT NFKD-decompose to ASCII+marks -- without this map they'd be silently
# DROPPED ('Łysa'->'ysa', 'Ægean'->'gean'), corrupting names and splitting the same stone across
# scrapes. Transliterate them before the ASCII pass.
_TRANSLIT = str.maketrans({
    "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
    "Đ": "D", "đ": "d", "Ð": "D", "ð": "d", "Þ": "TH", "þ": "th", "ß": "ss", "ẞ": "SS",
    "Ħ": "H", "ħ": "h", "Ŧ": "T", "ŧ": "t", "ı": "i", "İ": "I", "Ŋ": "N", "ŋ": "n",
})


def _drop_marks(text: str, form: str) -> str:
    """Decompose `text` in Unicode normal `form` and remove every mark (category M*): accents and
    combining signs carry no letter of their own."""
    return "".join(c for c in unicodedata.normalize(form, text) if not unicodedata.category(c).startswith("M"))


# Typographic forms unified BEFORE anything else, so a curly and a straight apostrophe never give two names (report
# 21, defect 1.3.2). The minus sign is a hyphen, a zero-width space marks a word break, and the micro sign is written
# 'u' in ASCII ('120um'). Every other separator is decided by its category (_separator).
_TYPOGRAPHIC = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u02bc": "'", "\u00b4": "'", "`": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2212": "-", "\u200b": " ", "\u00b5": "u",
})


def _prepared(text: str) -> str:
    """The ONE preparation both match_key and the display rules run first, so they can never disagree: typographic
    forms unified, separators decided on the ORIGINAL character (decomposed first, '\u2122' would become the
    letters 'TM' and join the word), compatibility forms decomposed and every mark dropped, separators again for
    what decomposition yields ('\u00bd' is '1\u20442'), then format characters removed and every whitespace or
    control character made a space. Afterwards the only punctuation left is ASCII."""
    separated = _separated(_drop_marks(_separated(text.translate(_TYPOGRAPHIC)), "NFKD"))
    return _visible(separated)


def _separated(text: str) -> str:
    """Every non-ASCII punctuation, symbol, private-use or unassigned character separates words: a dash becomes a
    hyphen, a compatibility form of one ASCII punctuation character becomes that character ('\uff0f' is '/'), and
    any other one a space. Letters, digits, marks, spaces, format and control characters pass through."""
    return "".join(_separator(c) for c in text)


_ASCII_PUNCT = re.compile(r"[!-/:-@\[-`{-~]")


def _separator(c: str) -> str:
    if c.isascii():
        return c
    category = unicodedata.category(c)
    if category == "Pd":
        return "-"
    if category[0] in "PS":
        ascii_form = unicodedata.normalize("NFKD", c).translate(_TYPOGRAPHIC)
        return ascii_form if _ASCII_PUNCT.fullmatch(ascii_form) else " "
    return " " if category in ("Co", "Cn", "Cs") else c



def _visible(text: str) -> str:
    """Every whitespace or control character becomes a space and every format character is removed: an invisible
    character (soft hyphen, word joiner) never splits a word, and an unusual space (U+2028) or a control character
    (U+0092, cp1252's curly apostrophe read as Latin-1) never glues two."""
    return "".join(" " if c.isspace() or unicodedata.category(c) == "Cc" else c for c in text
                   if unicodedata.category(c) != "Cf")


def ascii_fold(text: str) -> str:
    """Fold accented/special Latin letters to plain ASCII so a variety name never carries a
    special character that splits the same stone in two -- 'Rosa Porriño' == 'Rosa Porrino',
    'São Gabriel' == 'Sao Gabriel', 'Łysa' == 'Lysa'. NFKD decomposes accents and compatibility
    forms and every mark is dropped; THEN the non-decomposing letters (Æ/Œ/Ø/Ł/ß...) are
    transliterated, so a letter such as Ǣ or Ǿ (Æ or Ø carrying a mark) is not lost; any remaining
    non-ASCII is removed. Applied in slug, title-case, and the match key so the folding is
    consistent everywhere (scrapes that use accents/ligatures match those that don't)."""
    text = text or ""
    if text.isascii():
        return text
    return _drop_marks(text, "NFKD").translate(_TRANSLIT).encode("ascii", "ignore").decode("ascii")


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


# Every non-alphanumeric character -- whitespace, underscore, AND all punctuation (hyphen/dash,
# slash, "'", '.', ',', '(', ')', '+', unicode dashes/punct, ...) -- folds to ONE space ('&' reads 'and' first), so
# NEITHER a separator NOR any punctuation ever forks identity. `\w` keeps unicode letters (folded
# later by ascii_fold) and underscore, so underscore is added explicitly.
_NON_ALNUM = re.compile(r"[^\w\s]|_", re.UNICODE)


def match_key(text: str) -> str:
    """The ONE canonical key for matching any name -- stone type, colour, finish, quality, variety,
    vendor, country, port. Folds every non-alphanumeric run (whitespace, separators, AND all
    punctuation) to a single space, then folds accents to ASCII and casefolds. A separator, case,
    accent, OR punctuation difference must NEVER split the same name in two, so every name lookup
    and index in the pipeline keys on this -- and it agrees with matching.projections.norm:
    'Semi-Precious Stone' == 'semi_precious_stone' == 'Semi Precious Stone', and
    'St. Laurent' == 'St Laurent', 'Rosa Porriño' == 'Rosa Porrino'. '&' reads 'and', exactly as a cleaned name
    writes it (owner D1, 2026-11-04), so 'Black & Gold' == 'Black and Gold' and a scraped spelling keeps matching
    its own cleaned display.

    Punctuation is folded BEFORE ascii_fold, so a unicode dash/punct becomes a space rather than
    being stripped to nothing (which would glue the two words together). Marks are dropped FIRST:
    a mark (accent, combining sign) is not \\w, so the punctuation pass would turn 'n' + U+0303 into a
    space and fork the stone. Decomposed input (NFD, common from macOS pages and PDFs) carries such
    marks, and NFC cannot compose a mark that has no precomposed letter (U+0331), so the text is
    decomposed and every mark removed instead (skipped for plain ASCII text, which carries no mark: the hot
    path). This is the one shared normalizer; per-module copies MUST delegate here, not roll a weaker variant."""
    text = text or ""
    if not text.isascii():
        text = _prepared(text)
    return " ".join(ascii_fold(_NON_ALNUM.sub(" ", text.replace("&", " and "))).casefold().split())


class UnfoldableName(ValueError):
    """A name carries a letter with no ASCII form (a non-Latin script): it is never stored half-folded. The
    caller decides by context: an alias is dropped and reported, a variety name goes to review."""


# The owner's two display rules (2026-11-03): a NAME is ASCII letters, digits, spaces, hyphens and apostrophes;
# an IDENTIFIER (a grade such as 'AA/BB', a finish or standard code, a title) keeps printable ASCII punctuation.
_NOT_NAME = re.compile(r"[^A-Za-z0-9 '-]+")
_HYPHEN_RUN = re.compile(r"\s*-[\s-]*")
_APOSTROPHE_RUN = re.compile(r"'{2,}")
_LOOSE_APOSTROPHE = re.compile(r"(?<![A-Za-z0-9])'|'(?![A-Za-z0-9])")


def _ascii_display(text: str) -> str:
    """The shared part of both display rules: the one preparation match_key runs, then accents and compatibility
    forms folded. Raises UnfoldableName when a letter has no ASCII form (a modifier letter is folded or dropped,
    as match_key does): never stored half-folded."""
    prepared = _prepared(text or "")
    if any(c.isalpha() and unicodedata.category(c) != "Lm" and not ascii_fold(c) for c in prepared):
        raise UnfoldableName(f"no ASCII form for a letter in {text!r}")
    return ascii_fold(prepared)


def clean_name(text: str) -> str:
    """The display rule for a NAME (variety, alias, type, colour, origin, port, category): ASCII letters, digits,
    spaces, hyphens and apostrophes. '&' reads 'and', every other punctuation or symbol becomes a space, hyphen
    runs collapse, and an apostrophe is kept only inside a word (one, never a run). Every step mirrors match_key, so
    the cleaned name always keys like the original. Case is the caller's.
    Bracketed alternatives are split off by the caller first (split_bracket_alias). Expectations live in the shared
    name-key vectors (`display`)."""
    named = _NOT_NAME.sub(" ", _ascii_display(text).replace("&", " and "))
    named = _LOOSE_APOSTROPHE.sub(" ", _APOSTROPHE_RUN.sub("'", named))
    return " ".join(_HYPHEN_RUN.sub("-", named).split()).strip("- ")


def clean_identifier(text: str) -> str:
    """The display rule for an IDENTIFIER (a quality grade such as 'AA/BB', a finish or standard code, a title):
    printable ASCII with its punctuation kept, whitespace collapsed. Keys like the original (match_key). The kind
    of each field comes from the backend's field-kinds.json (reference/loaders/field_kinds.py). Expectations live in
    the shared name-key vectors (`identifier`)."""
    return " ".join(_ascii_display(text).split())


# The display rule for an ALIAS (field kind "alias", 34's OWNED-NAMES-CLEAN row; the owner's ruling, 13 Nov, with
# Mas's refusal (b)): every letter of every script is kept as written (accents never folded, a non-Latin script never
# dropped), so the stored class is the database's ^[[:alpha:]̀-ͯ0-9 '-]+$.
_ALIAS_TYPOGRAPHIC = str.maketrans({
    "`": "'", "´": "'", "‘": "'", "’": "'", "‚": "'", "‛": "'", "ʼ": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "−": "-",
})
_ALIAS_NON_LETTERS = frozenset("0123456789 '-")
_ALIAS_MARKS = range(0x0300, 0x0370)            # the combining diacritical marks block, kept as written
_ASCII_DIGITS = frozenset("0123456789")


class AliasRefused(ValueError):
    """An alias that is not stored at all (Mas's ruling (b)): mojibake ('?' or U+FFFD between two letters, a lost
    letter such as 'K?r?ehir'). The caller lists it for the owner (review/aliases_dropped.csv); where the real spelling
    is recoverable the fix belongs at the alias's source, not in the cleaner."""


def _mojibake(text: str) -> bool:
    """'?' or U+FFFD with a letter (any script) on BOTH sides: 'C?te' and 'K?r?ehir' are, 'Picass?' and 'Who? Me' are
    not (their '?' becomes a space)."""
    return any(c in "?�" and 0 < i < len(text) - 1 and text[i - 1].isalpha() and text[i + 1].isalpha()
               for i, c in enumerate(text))


def _is_letter(c: str) -> bool:
    return unicodedata.category(c).startswith("L")


def _alias_char(c: str) -> bool:
    return _is_letter(c) or ord(c) in _ALIAS_MARKS or c in _ALIAS_NON_LETTERS


def _drop_loose_alias_apostrophes(text: str) -> str:
    """Each apostrophe kept only inside a word: a letter (any script), a kept combining mark or an ASCII digit before
    it AND a letter or an ASCII digit after it; any other apostrophe becomes a space."""
    def inside(i: int) -> bool:
        before, after = text[i - 1] if i else "", text[i + 1] if i + 1 < len(text) else ""
        return (bool(before) and (_is_letter(before) or ord(before) in _ALIAS_MARKS or before in _ASCII_DIGITS)
                and bool(after) and (_is_letter(after) or after in _ASCII_DIGITS))
    return "".join(" " if c == "'" and not inside(i) else c for i, c in enumerate(text))


def clean_alias(text: str) -> str:
    """The display rule for an ALIAS, step for step (34's spec): REFUSED first (AliasRefused) when the raw value holds
    mojibake (_mojibake); then (1) every format character removed (a zero-width space goes, never a space); (2) the
    alias typographic table (a backtick, an acute or a curly quote is an apostrophe) and every dash a hyphen; (3) NFC,
    accents kept; (4) '&' reads 'and'; (5) every character that is not a letter (any script), a combining mark in
    U+0300-U+036F, an ASCII digit, a space, an apostrophe or a hyphen becomes a space; (6) apostrophe runs collapse,
    an apostrophe outside a word becomes a space (_drop_loose_alias_apostrophes), then the name's hyphen rules,
    whitespace collapsed. '' when nothing is left: the caller refuses it, it is never stored. Case is the caller's.
    Expectations live in the shared name-key vectors (`alias`)."""
    text = text or ""
    if _mojibake(text):
        raise AliasRefused(f"{text!r} holds mojibake ('?' or U+FFFD between two letters)")
    visible = "".join(c for c in text if unicodedata.category(c) != "Cf")
    typed = "".join("-" if unicodedata.category(c) == "Pd" else c for c in visible.translate(_ALIAS_TYPOGRAPHIC))
    joined = unicodedata.normalize("NFC", typed).replace("&", " and ")
    kept = "".join(c if _alias_char(c) else " " for c in joined)
    kept = _drop_loose_alias_apostrophes(_APOSTROPHE_RUN.sub("'", kept))
    return " ".join(_HYPHEN_RUN.sub("-", kept).split()).strip("- ")

_HTML_RE = re.compile(r"<[A-Za-z/!][^>]*>|&[A-Za-z]+;|&#\d+;")
_PROSE_WORD_RE = re.compile(r"[a-z']+")


def clean_description(text: str, function_words, min_share: float) -> str | None:
    """A supplier description as stored prose, or None when it is not plain English prose (owner decision O3): NFC,
    format characters removed, line breaks unified to '\n', any other control character a space, spacing otherwise
    kept. None for an HTML tag or entity, or when the share of English `function_words` among its words is below
    `min_share` (another language, an internal note); the caller then uses the generated description."""
    text = unicodedata.normalize("NFC", text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(" " if unicodedata.category(c) == "Cc" and c != "\n" else c for c in text
                   if unicodedata.category(c) != "Cf").strip()
    if not text or _HTML_RE.search(text):
        return None
    words = _PROSE_WORD_RE.findall(text.casefold())
    if not words or sum(w in function_words for w in words) / len(words) < min_share:
        return None
    return text



# ASCII and fullwidth round and square brackets
_BRACKET_ALIAS_RE = re.compile("[(\\[\uff08\uff3b]\\s*([^)\\]\uff09\uff3d]+?)\\s*[)\\]\uff09\uff3d]")


def split_bracket_alias(name: str) -> tuple[str, list[str]]:
    """Pull any '(alternative)' or '[alternative]' out of a variety name and return
    (clean_name, [aliases]). 'Black Galaxy (Star Galaxy)' -> ('Black Galaxy', ['Star Galaxy']),
    'Verde Ubatuba (Ubatuba)' -> ('Verde Ubatuba', ['Ubatuba']). The bracketed part is a local or
    alternative spelling that belongs in the alias list, not the display name. Returns the name
    unchanged and [] when there is no bracket. Handles multiple brackets."""
    text = name or ""
    aliases = [m.strip() for m in _BRACKET_ALIAS_RE.findall(text) if m.strip()]
    if not aliases:
        return text.strip(), []
    return collapse_ws(_BRACKET_ALIAS_RE.sub(" ", text)), aliases


# A '(In) <place> Market <: | called/known/named as | called:> ' context prefix some imported aliases carry
# before the actual local name, e.g. 'In China Market:(bai Si Hui Wang)', 'In Local Market: (Dolomit
# Arbon', 'In China Stone Market Callled As (Caiyun Zhui Yue', 'In China Local Market Called: Bai Yu'. 'market'
# must be followed by a colon or an explicit 'as' phrase, so a normal name that merely contains the word
# 'market' is never stripped.
_MARKET_CTX_RE = re.compile(
    r"^\s*\(?\s*(?:in\s+)?(?:\w+\s+){0,3}market\b\s*(?::|(?:cal+led|known|named)\s*(?:as\b|:))\s*[-(]?\s*", re.I)
_EMPTY_BRACKETS_RE = re.compile("[(\\[\uff08\uff3b]\\s*[)\\]\uff09\uff3d]")
# Every comma form one alias field may join several names with. The split comes BEFORE cleaning: cleaned whole,
# a blob with one non-Latin half would be refused and lose its Latin half too.
_ALIAS_SEPARATOR_RE = re.compile("[,\u060c\u3001\ufe10\ufe11\ufe50\ufe51\uff0c\uff64]")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


# The Latin script (Scripts.txt sc=Latn, the JS \p{Script=Latin}), from Unicode 18.0.0's Scripts.txt. Python has no
# script property. 18.0.0's set is a strict superset of 14.0.0's (the vectors' floor; no code point changed script),
# so it agrees with the floor on every vector input and with any runtime up to 18.0.0.
_LATIN_RANGES = (
    (0x0041, 0x005A), (0x0061, 0x007A), (0x00AA, 0x00AA), (0x00BA, 0x00BA), (0x00C0, 0x00D6), (0x00D8, 0x00F6),
    (0x00F8, 0x02B8), (0x02E0, 0x02E4), (0x1D00, 0x1D25), (0x1D2C, 0x1D5C), (0x1D62, 0x1D65), (0x1D6B, 0x1D77),
    (0x1D79, 0x1DBE), (0x1E00, 0x1EFF), (0x2071, 0x2071), (0x207F, 0x207F), (0x2090, 0x209F), (0x212A, 0x212B),
    (0x2132, 0x2132), (0x214E, 0x214E), (0x2160, 0x2188), (0x2C60, 0x2C7F), (0xA722, 0xA787), (0xA78B, 0xA7DD),
    (0xA7E2, 0xA7E2), (0xA7F1, 0xA7FF), (0xAB30, 0xAB5A), (0xAB5C, 0xAB64), (0xAB66, 0xAB69), (0xAB6C, 0xAB6D),
    (0xFB00, 0xFB06), (0xFF21, 0xFF3A), (0xFF41, 0xFF5A), (0x10780, 0x10785), (0x10787, 0x107B0), (0x107B2, 0x107BF),
    (0x1DF00, 0x1DF81), (0x1DF90, 0x1DF96), (0x1DFCD, 0x1DFF2), (0x1DFF5, 0x1DFFF),
)


def _is_latin(c: str) -> bool:
    cp = ord(c)
    return any(lo <= cp <= hi for lo, hi in _LATIN_RANGES)


def keeps_script(text: str) -> bool:
    """True when the RAW value has a letter outside the Latin script: its alias_key keeps the script (the second
    path) instead of folding to match_key."""
    return any(_is_letter(c) and not _is_latin(c) for c in text or "")


def _alias_key_char(c: str) -> bool:
    category = unicodedata.category(c)
    return category[0] in "LM" or category == "Nd"


def alias_key(text: str) -> str:
    """THE key of an alias value (Mas's ruling, 13 Nov; the backend's paired aliasKey, `alias_key` in the shared
    name-key vectors): match_key when the RAW value has no letter outside the Latin script (Latin with diacritics
    keys as matching sees it, so every all-Latin key is unchanged). Otherwise the value keeps its script: every format
    character removed, NFKC, lower() (the default lowercase mapping JS toLowerCase also applies; NOT casefold, which
    JS lacks: a final sigma stays), every character that is not a letter, a mark or a decimal digit (any script) a
    space, whitespace collapsed. '' when nothing is left: the callers refuse it. Pure; never raises."""
    text = text or ""
    if not keeps_script(text):
        return match_key(text)
    visible = "".join(c for c in text if unicodedata.category(c) != "Cf")
    lowered = unicodedata.normalize("NFKC", visible).lower()
    return " ".join("".join(c if _alias_key_char(c) else " " for c in lowered).split())


def clean_alias_list(name: str, raw_aliases, clean) -> tuple[list[str], list[str]]:
    """Normalize a variant's alias list into clean, de-duplicated alternative names:
      - split a blob on every comma form ('Tropical Rain Forest, Rain Forest'), before cleaning,
      - drop a 'China market:' context prefix and pull any '(local name)' out of the brackets,
      - strip stray wrapping punctuation/brackets and junk (an alias with no letters or digits),
      - clean each piece with `clean` (the alias field's display rule, which the caller passes in, so this module
        holds no kind),
      - drop any alias equal to the variant Name (redundant) and de-duplicate on alias_key (match_key for an
        all-Latin alias), first seen kept.
    Returns (kept, dropped): a piece with no ASCII form (UnfoldableName, under the name kind) or a refused alias
    (AliasRefused, under the alias kind) is dropped and returned for reporting."""
    name_key = match_key(name or "")
    kept: list[str] = []
    dropped: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = collapse_ws((value or "").strip(" ()[]{}:;-/.,").strip())
        if not _ALNUM_RE.search(value) and not any(c.isalpha() for c in value):
            return                                   # pure punctuation -> junk
        try:
            value = clean(value)
        except (UnfoldableName, AliasRefused):
            dropped.append(value)
            return
        key = alias_key(value)
        if not key or key == name_key or key in seen:
            return                                   # empty, == Name, or already have it
        seen.add(key)
        kept.append(value)

    for raw in raw_aliases or []:
        for piece in _ALIAS_SEPARATOR_RE.split(raw or ""):
            piece = _MARKET_CTX_RE.sub("", piece)
            piece = _EMPTY_BRACKETS_RE.sub(" ", piece)     # drop empty '()' / '[]' pairs
            base, bracketed = split_bracket_alias(piece)   # pull '(local name)' out as its own alias
            add(base)
            for b in bracketed:
                add(b)
    return kept, dropped


def _cap_word(word: str) -> str:
    """Capitalize the first alphabetic char, lowercase the rest -- so 'ASTORIA' ->
    'Astoria', 'no.' -> 'No.', '(lasa' -> '(Lasa', '426' -> '426'."""
    out, capped = [], False
    for ch in word:
        if not capped and ch.isalpha():
            out.append(ch.upper())
            capped = True
        else:
            out.append(ch.lower())
    return "".join(out)


# Romance-language connectors kept lower-case in a display title unless they LEAD the name
# (Giallo di Siena, Verde de Fantasia). Small and closed on purpose -- a display nicety, NOT a
# general stop-word list, and it never affects identity (match_key/gen_key fold separately).
_TITLE_CONNECTORS = frozenset({"de", "do", "da", "di", "del", "della", "e", "y"})


def title_case(text: str) -> str:
    """Every word capitalized, rest lower-case, whitespace collapsed: casing only. It decides no alphabet: a
    name is cleaned to ASCII by its field's rule first (field_kinds.display), and review-card prose may carry raw
    scraped text, which this never refuses. Romance connectors (de/do/di/...) stay lower-case unless they lead
    the name."""
    words = (text or "").split()
    return " ".join(
        w.lower() if i and w.lower() in _TITLE_CONNECTORS else _cap_word(w)
        for i, w in enumerate(words))


def slugify(text: str, sep: str = "-") -> str:
    """A slug from the name's key words (match_key), so a slug, a Key and the match key read a name ONE way: a curly
    and a straight apostrophe, or '&' and 'and', give one handle and one Key. `sep` is '-' for handles and '_' for
    Keys and identity slugs."""
    return sep.join(match_key(text).split())


# strip stray edge punctuation, but NOT brackets -- a balanced '(Sunset Gold)' is meaningful
_EDGE_PUNCT = re.compile(r"^[\s\-\u2013\u2014_/.,:;#*|]+|[\s\-\u2013\u2014_/.,:;#*|]+$")


def _kept_code_match(tok: str) -> bool:
    """True if `tok` matches the active pack's real-code pattern (stone: granite 'G682') -- a codey-LOOKING
    token that is a genuine name in this domain, so it is never stripped or flagged. False when the pack
    declares no such pattern (a non-stone domain then treats every digit-bearing token as a code)."""
    from stone_pipeline.config.domain import active_pack
    pat = active_pack().name_code_pattern
    return bool(pat) and re.fullmatch(pat, tok) is not None


def _is_number_code(tok: str) -> bool:
    """A token carrying a number that does NOT belong in a variety name of this domain -> drop it. The one
    exception is the pack's real-code pattern (stone: a granite code 'G682'/'G032'). EVERYTHING else with a
    digit ('3D', '2cm', '1.08', '426', '883') or a 'No.' series marker is a code/measurement to strip."""
    t = tok.strip("().[]{}-\u2013\u2014,")
    if not t:
        return False
    if _kept_code_match(t):                              # a domain-declared real code (granite 'G682') -> keep
        return False
    if t.casefold().rstrip(".") == "no":                 # the 'No.' in 'No. 426'
        return True
    return any(c.isdigit() for c in t)                   # any other digit-bearing token -> drop


# Real short leading WORDS that front legitimate multi-word variety names -- they read like names, not
# supplier codes, so they must not be shape-flagged just for being 2 chars. The romance articles/particles
# (El, La, Di ...) all carry a vowel and are handled by the vowel test; this set is only the vowel-LESS
# real abbreviations (Saint/Mount/Fort) that would otherwise look code-shaped ('St Laurent', 'Mt Blanc').
_REAL_SHORT_LEADS = frozenset({"st", "mt", "ft"})


def looks_codey(tok: str) -> bool:
    """A token that does NOT read like a real name word -- used to spot supplier codes by SHAPE, not by
    enumerating them: it contains a digit, or is un-pronounceable (no vowel), or is a bare <=2-char token
    that is neither a vowel-bearing word ('El', 'La', 'Di') nor a known short lead word ('St', 'Mt', 'Ft').
    A 2-char lead with a vowel or a real abbreviation is a NAME word ('El Dorado', 'St Laurent'), not a
    code, so it is never stripped."""
    t = tok.strip("().[]{}-\u2013\u2014,.")
    if not t:
        return False
    if any(c.isdigit() for c in t):
        return True                                      # any digit-bearing token -> code-shaped
    has_vowel = any(c.casefold() in "aeiou" for c in t)
    if len(t) <= 2:
        # a 2-char lead is a supplier code only if it does NOT read like a word: no vowel AND not a known
        # short lead ('Z', 'ZB', 'Xy' -> code; 'El', 'La', 'St', 'Mt' -> real lead, kept).
        return not has_vowel and t.casefold() not in _REAL_SHORT_LEADS
    return not has_vowel                                 # 3+ chars with no vowel -> un-pronounceable -> code


def detect_code_prefixes(names: list[str], min_fanout: int = 2) -> frozenset[str]:
    """DISCOVER a source's leading code prefixes from the data, with NO hardcoded strings: a
    code-shaped leading token that fans out across >= min_fanout DISTINCT varieties is a code
    (varsha's 'Z' fronts 61 names, 'ZB' fronts 2), while a real shared word ('Verde', 'Blue')
    is not code-shaped and a one-off prefix ('Mt Blanc') doesn't fan out. Returns casefolded
    tokens to strip. This generalises to any new scraper's codes without listing them."""
    fanout: dict[str, set] = {}
    for n in names:
        toks = (n or "").split()
        if len(toks) >= 2 and looks_codey(toks[0]):
            fanout.setdefault(toks[0].casefold(), set()).add(" ".join(toks[1:]).casefold())
    return frozenset(h for h, rest in fanout.items() if len(rest) >= min_fanout)


def clean_variety_name(name: str, code_prefixes: tuple[str, ...] = (),
                       lead_codes: frozenset[str] = frozenset()) -> str:
    """Strip supplier-code artifacts from a variety name, GENERICALLY -- so new scrapes with
    their own junk are handled without bespoke logic:
      1. source-declared prefix patterns (`code_prefixes`, optional explicit override),
      2. stray surrounding punctuation,
      3. a LEADING run of code tokens: lone letters ('Z','A') OR data-discovered codes
         (`lead_codes` from detect_code_prefixes, e.g. 'zb') -- never the last token,
      4. SEPARATE number / series codes anywhere ('Super – 1.08'->'Super', 'Marjan – No. 426'->
         'Marjan', '883 Black'->'Black') -- but granite's attached 'G682'/'G032' stay.
    'La Perla', 'El Dorado', 'Mt Blanc' untouched. Never returns empty."""
    n = (name or "").strip()
    for pat in code_prefixes:
        n = re.sub(pat, "", n, flags=re.IGNORECASE).strip()
    n = _EDGE_PUNCT.sub("", n).strip()
    toks = n.split()
    i = 0
    while i < len(toks) - 1 and (
            (len(toks[i]) == 1 and toks[i].isalpha()) or toks[i].casefold() in lead_codes):
        i += 1
    toks = [t for t in toks[i:] if not _is_number_code(t)]                # drop loose numbers
    # strip ONE trailing lone-letter grade code so a graded product collapses to its base variety
    # ('Rosal C'/'Rosal T' -> 'Rosal'; customer finds it under 'Rosal', tells them apart by colour).
    # Space-separated and hyphen-attached ('Red Wendeng-z' -> 'Red Wendeng'). Only ONE letter (no
    # cascade: 'Bianco C T' -> 'Bianco C', not 'Bianco') and KEEP a Roman-numeral 'I' grade ('Onyx I'
    # stays distinct from 'Onyx II'). Only when the domain grades by a trailing letter (pack-declared);
    # stone's backbone is validated to have no variety ending in a lone non-'I' letter.
    from stone_pipeline.config.domain import active_pack
    if active_pack().trailing_grade_letters:
        if len(toks) >= 2 and len(toks[-1]) == 1 and toks[-1].isalpha() and toks[-1].upper() != "I":
            toks = toks[:-1]
        if toks:
            m = re.search(r"-([A-Za-z])$", toks[-1])
            if m and m.group(1).upper() != "I":
                toks[-1] = toks[-1][:m.start()] or toks[-1]               # 'Wendeng-z' -> 'Wendeng'
    toks = [t for t in toks if t.strip("-\u2013\u2014_/|")]                         # drop dangling separators
    return _EDGE_PUNCT.sub("", " ".join(toks)).strip() or n or (name or "").strip()


def looks_like_artifact(name: str) -> bool:
    """A HIGH-CONFIDENCE check that a name does not read like a real stone -- a backstop to the
    per-source corpus cleaning (detect_code_prefixes), used to refuse minting and keep junk out
    of the upload files. Only the unambiguous shapes: empty / too short / no letters, a leading
    LONE letter ('Z Astoria'), or a SEPARATE number/series code ('Super – 1.08', 'Marjan No. 426',
    '883 Black'). Granite's attached 'G682' is NOT flagged. Ambiguous short codes are left to the
    corpus cleaner + the review queue rather than guessed at here."""
    n = (name or "").strip()
    if len(n) < 2 or not any(c.isalpha() for c in n):
        return True                                     # empty, single char, or no letters
    if not any(c.isalpha() for c in ascii_fold(n)):
        return True                                     # only NON-Latin letters (CJK/Cyrillic): they fold to
                                                        # an empty match_key, so two such names would mint the
                                                        # SAME uuid5 Key and collapse -- route to review instead
    toks = n.split()
    if any(_is_number_code(t) for t in toks):
        return True                                     # carries a loose number/series code
    if len(toks) >= 2 and len(toks[0]) == 1 and toks[0].isalpha():
        return True                                     # leading lone-letter code: 'Z Astoria'
    return False


def looks_code_shaped(name: str) -> str:
    """If `name` is shaped like a supplier code rather than a variety, return WHY ('lone_letter'
    or 'bare_code'); else "". These are routed to review (confirm), never minted or merged:
      - trailing lone-letter grade code: 'Rosal C', 'Trani Bianco H', 'Colonial White - M', 'Red Wendeng-z'
      - bare short code: 'Gs', 'Zb'  (granite G+number names like 'G682' are real -> kept)
    Colours are NEVER touched here (Agata Black vs Agata Blue stay distinct)."""
    from stone_pipeline.config.domain import active_pack
    toks = [t for t in re.split(r"[\s\-]+", (name or "").strip()) if t]
    if not toks:
        return ""
    if active_pack().trailing_grade_letters and len(toks) >= 2 and len(toks[-1]) == 1 and toks[-1].isalpha():
        return "lone_letter"
    # a SINGLE token that is code-SHAPED (no vowel or <=2 chars -> un-pronounceable: 'Gs','Zb','Lg')
    # but NOT a real short name ('Ice','Oak','Ash' have a vowel) and NOT a pack real-code (granite 'G682').
    if len(toks) == 1 and not _kept_code_match(toks[0]) and looks_codey(toks[0]):
        return "bare_code"
    return ""


# The indefinite article goes by the SOUND of the next word, which the letter rule misses in two ways. This is English
# language data, not a tunable: a vowel letter read as a consonant sound ("a uniform", "a European", "a UV-cured", "a
# one-off"; "one" as a word only, so "an onerous" keeps the letter rule), and a silent h ("an hour", "an honest"). Each
# is matched on the first word, case-insensitively, as a prefix ("unique", "hourly", "heirloom").
_CONSONANT_SOUND_PREFIXES = ("uni", "use", "eu", "uv")
_CONSONANT_SOUND_WORDS = ("one",)
_VOWEL_SOUND_H_PREFIXES = ("hour", "honest", "honour", "heir")


def indefinite_article(phrase: str) -> str:
    """'an' when `phrase` starts with a vowel sound, else 'a' ("an antique slab", "a honed slab", "an onyx", "a
    uniform finish", "an hour"). A phrase with no word has no article: ValueError."""
    words = phrase.split()
    if not words:
        raise ValueError("an article needs a word after it")
    word = words[0].casefold()
    if word.startswith(_VOWEL_SOUND_H_PREFIXES):
        return "an"
    if word.startswith(_CONSONANT_SOUND_PREFIXES) or word.split("-")[0] in _CONSONANT_SOUND_WORDS:
        return "a"
    return "an" if word[0] in "aeiou" else "a"
