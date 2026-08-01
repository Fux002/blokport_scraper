"""Small deterministic text helpers for generation (section 10.3, 10.4)."""

from __future__ import annotations

import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


# Latin letters that do NOT NFKD-decompose to ASCII+marks -- without this map they'd be silently
# DROPPED ('Łysa'->'ysa', 'Ægean'->'gean'), corrupting names and splitting the same stone across
# scrapes. Transliterate them before the ASCII pass.
_TRANSLIT = str.maketrans({
    "Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "Ø": "O", "ø": "o", "Ł": "L", "ł": "l",
    "Đ": "D", "đ": "d", "Ð": "D", "ð": "d", "Þ": "TH", "þ": "th", "ß": "ss", "ẞ": "SS",
    "Ħ": "H", "ħ": "h", "Ŧ": "T", "ŧ": "t", "ı": "i", "İ": "I", "Ŋ": "N", "ŋ": "n",
})


def ascii_fold(text: str) -> str:
    """Fold accented/special Latin letters to plain ASCII so a variety name never carries a
    special character that splits the same stone in two -- 'Rosa Porriño' == 'Rosa Porrino',
    'São Gabriel' == 'Sao Gabriel', 'Łysa' == 'Lysa'. Non-decomposing letters (Æ/Œ/Ø/Ł/ß...) are
    transliterated first; then NFKD decomposes accents, combining marks are dropped, and any
    remaining non-ASCII is removed. Applied in slug, title-case, and the match key so the folding
    is consistent everywhere (scrapes that use accents/ligatures match those that don't)."""
    decomposed = unicodedata.normalize("NFKD", (text or "").translate(_TRANSLIT))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.encode("ascii", "ignore").decode("ascii")


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


# Every non-alphanumeric character -- whitespace, underscore, AND all punctuation (hyphen/dash,
# slash, '&', "'", '.', ',', '(', ')', '+', unicode dashes/punct, ...) -- folds to ONE space, so
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
    'Black & Gold' == 'Black Gold', 'St. Laurent' == 'St Laurent', 'Rosa Porriño' == 'Rosa Porrino'.

    Punctuation is folded BEFORE ascii_fold, so a unicode dash/punct becomes a space rather than
    being stripped to nothing (which would glue the two words together). This is the one shared
    normalizer; per-module copies MUST delegate here, not roll a weaker variant."""
    return " ".join(ascii_fold(_NON_ALNUM.sub(" ", text or "")).casefold().split())


_BRACKET_ALIAS_RE = re.compile(r"[\(\[]\s*([^\)\]]+?)\s*[\)\]]")


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


# A '(In) <place> Market <: | called/known/named as> ' context prefix some imported aliases carry
# before the actual local name, e.g. 'In China Market:(bai Si Hui Wang)', 'In Local Market: (Dolomit
# Arbon', 'In China Stone Market Callled As (Caiyun Zhui Yue'. 'market' must be followed by a colon or
# an explicit 'as' phrase, so a normal name that merely contains the word 'market' is never stripped.
_MARKET_CTX_RE = re.compile(
    r"^\s*\(?\s*(?:in\s+)?(?:\w+\s+){0,3}market\b\s*(?::|(?:cal+led|known|named)\s+as)\s*[-(]?\s*", re.I)
_EMPTY_BRACKETS_RE = re.compile(r"[\(\[]\s*[\)\]]")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def clean_alias_list(name: str, raw_aliases) -> list[str]:
    """Normalize a variant's alias list into clean, de-duplicated alternative names:
      - split a comma-joined blob into separate aliases ('Tropical Rain Forest, Rain Forest'),
      - drop a 'China market:' context prefix and pull any '(local name)' out of the brackets,
      - strip stray wrapping punctuation/brackets and junk (an alias with no letters or digits),
      - drop any alias equal to the variant Name (redundant),
      - title-case for DISPLAY (accents preserved) but de-duplicate on match_key, so a case- OR
        accent-only difference (Rosé vs Rose) still collapses. First-seen order preserved.
    Returns the cleaned alias list (possibly empty). title_case is resolved at call time."""
    name_key = match_key(name or "")
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = collapse_ws((value or "").strip(" ()[]{}:;-/.,").strip())
        if not _ALNUM_RE.search(value):
            return                                   # pure punctuation -> junk
        value = title_case(value)
        key = match_key(value)
        if not key or key == name_key or key in seen:
            return                                   # empty, == Name, or already have it
        seen.add(key)
        out.append(value)

    for raw in raw_aliases or []:
        for piece in (raw or "").split(","):         # one alias may hold several comma-joined names
            piece = _MARKET_CTX_RE.sub("", piece)
            piece = _EMPTY_BRACKETS_RE.sub(" ", piece)     # drop empty '()' / '[]' pairs
            base, bracketed = split_bracket_alias(piece)   # pull '(local name)' out as its own alias
            add(base)
            for b in bracketed:
                add(b)
    return out


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
    """Every word capitalized, rest lower-case, whitespace collapsed. Unicode-aware: source accents
    are PRESERVED (Rosé stays Rosé, Macaúbas stays Macaúbas) so the display name reads as the name,
    while IDENTITY folds accents separately (match_key / gen_key's slug), so 'Porriño' and 'Porrino'
    are still one stone. Romance connectors (de/do/di/...) stay lower-case unless they lead the name.
    Note: this preserves accents that are IN the source; it does not restore a missing one
    (AZUL MACAUBAS -> Azul Macaubas, not Macaúbas -- that needs a canonical/synonym table)."""
    words = (text or "").split()
    return " ".join(
        w.lower() if i and w.lower() in _TITLE_CONNECTORS else _cap_word(w)
        for i, w in enumerate(words))


def slugify(text: str) -> str:
    return _NON_SLUG.sub("-", ascii_fold(text or "").casefold()).strip("-")


# strip stray edge punctuation, but NOT brackets -- a balanced '(Sunset Gold)' is meaningful
_EDGE_PUNCT = re.compile(r"^[\s\-–—_/.,:;#*|]+|[\s\-–—_/.,:;#*|]+$")


_GRANITE_CODE = re.compile(r"[Gg]\d+[A-Za-z]?")  # the ONE number allowed: granite 'G682', 'G032'


def _is_number_code(tok: str) -> bool:
    """A token carrying a number that does NOT belong in a stone variety name -> drop it. The
    ONLY number a stone name uses is a granite code: 'G' + digits ('G682', 'G032'). EVERYTHING
    else with a digit ('3D', '2cm', '1.08', '426', '883') or a 'No.' series marker is a code /
    measurement to strip -- no other stone uses numbers in its name."""
    t = tok.strip("().[]{}-–—,")
    if not t:
        return False
    if _GRANITE_CODE.fullmatch(t):                       # granite 'G682' is a real name -> keep
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
    t = tok.strip("().[]{}-–—,.")
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
    # stays distinct from 'Onyx II'). Validated: no backbone variety ends in a lone non-'I' letter.
    if len(toks) >= 2 and len(toks[-1]) == 1 and toks[-1].isalpha() and toks[-1].upper() != "I":
        toks = toks[:-1]
    if toks:
        m = re.search(r"-([A-Za-z])$", toks[-1])
        if m and m.group(1).upper() != "I":
            toks[-1] = toks[-1][:m.start()] or toks[-1]                   # 'Wendeng-z' -> 'Wendeng'
    toks = [t for t in toks if t.strip("-–—_/|")]                         # drop dangling separators
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
    toks = [t for t in re.split(r"[\s\-]+", (name or "").strip()) if t]
    if not toks:
        return ""
    if len(toks) >= 2 and len(toks[-1]) == 1 and toks[-1].isalpha():
        return "lone_letter"
    # a SINGLE token that is code-SHAPED (no vowel or <=2 chars -> un-pronounceable: 'Gs','Zb','Lg')
    # but NOT a real short name ('Ice','Oak','Ash' have a vowel) and NOT granite 'G682'.
    if len(toks) == 1 and not _GRANITE_CODE.fullmatch(toks[0]) and looks_codey(toks[0]):
        return "bare_code"
    return ""
