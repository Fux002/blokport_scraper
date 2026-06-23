"""Small deterministic text helpers for generation (section 10.3, 10.4)."""

from __future__ import annotations

import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_WS = re.compile(r"\s+")


def ascii_fold(text: str) -> str:
    """Fold accented/special Latin letters to plain ASCII so a variety name never carries a
    special character that splits the same stone in two -- 'Rosa Porriño' == 'Rosa Porrino',
    'São Gabriel' == 'Sao Gabriel'. NFKD decomposes the accent, the combining marks are dropped,
    and any remaining non-ASCII is removed. Applied in slug, title-case, and the match key so the
    folding is consistent everywhere (scrapes that use accents match those that don't)."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.encode("ascii", "ignore").decode("ascii")


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", (text or "").strip())


def _cap_word(word: str) -> str:
    """Capitalize the first alphabetic char, lowercase the rest — so 'ASTORIA' ->
    'Astoria', 'no.' -> 'No.', '(lasa' -> '(Lasa', '426' -> '426'."""
    out, capped = [], False
    for ch in word:
        if not capped and ch.isalpha():
            out.append(ch.upper())
            capped = True
        else:
            out.append(ch.lower())
    return "".join(out)


def title_case(text: str) -> str:
    """Every word capitalized, rest lower-case, whitespace collapsed, accents folded to ASCII.
    Consistent variant names and titles regardless of how the source cased or accented them."""
    return collapse_ws(" ".join(_cap_word(w) for w in ascii_fold(text or "").split()))


def slugify(text: str) -> str:
    return _NON_SLUG.sub("-", ascii_fold(text or "").casefold()).strip("-")


# strip stray edge punctuation, but NOT brackets — a balanced '(Sunset Gold)' is meaningful
_EDGE_PUNCT = re.compile(r"^[\s\-–—_/.,:;#*|]+|[\s\-–—_/.,:;#*|]+$")


_GRANITE_CODE = re.compile(r"[Gg]\d+[A-Za-z]?")  # the ONE number allowed: granite 'G682', 'G032'


def _is_number_code(tok: str) -> bool:
    """A token carrying a number that does NOT belong in a stone variety name -> drop it. The
    ONLY number a stone name uses is a granite code: 'G' + digits ('G682', 'G032'). EVERYTHING
    else with a digit ('3D', '2cm', '1.08', '426', '883') or a 'No.' series marker is a code /
    measurement to strip — no other stone uses numbers in its name."""
    t = tok.strip("().[]{}-–—,")
    if not t:
        return False
    if _GRANITE_CODE.fullmatch(t):                       # granite 'G682' is a real name -> keep
        return False
    if t.casefold().rstrip(".") == "no":                 # the 'No.' in 'No. 426'
        return True
    return any(c.isdigit() for c in t)                   # any other digit-bearing token -> drop


def looks_codey(tok: str) -> bool:
    """A token that does NOT read like a real name word: <=2 chars, contains a digit, or has
    no vowel (un-pronounceable). Used to spot supplier codes by SHAPE, not by enumerating them."""
    t = tok.strip("().[]{}-–—,.")
    if not t:
        return False
    return len(t) <= 2 or any(c.isdigit() for c in t) or not any(c.casefold() in "aeiou" for c in t)


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
    toks = [t for t in toks if t.strip("-–—_/|")]                         # drop dangling separators
    return _EDGE_PUNCT.sub("", " ".join(toks)).strip() or n or (name or "").strip()


def looks_like_artifact(name: str) -> bool:
    """A HIGH-CONFIDENCE check that a name does not read like a real stone — a backstop to the
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
