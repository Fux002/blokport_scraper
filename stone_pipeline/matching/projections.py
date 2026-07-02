"""Normalized projections for the matching engine (section 5A.1).

Each projection turns a class of surface difference into an exact-lookup hit:

  norm        casefold, strip punctuation, collapse spaces   case and punctuation
  compact     alnum only, no spaces                          spacing/concatenation
  tokenset    sorted token tuple                             word-order reversal
  deprefixed  strip inventory prefixes and trailing tags     supplier codes
  phonetic    metaphone per token                            spelling-by-ear typos

These are deterministic and reversible, so projection-exact matches are safe to
accept at high confidence (section 5A.2 tier 3).
"""

from __future__ import annotations

import re

import jellyfish

from stone_pipeline.core.text import ascii_fold, match_key

# inventory prefixes seen on supplier exports (Z, ZB, "Z B")
_INV_PREFIX = re.compile(r"^(z\s?b|z)\s+", flags=re.IGNORECASE)
# trailing render tags only: a trailing slash render/thickness tag or an explicit 'grade X'. A bare
# trailing letter (a/b/c/d) is NOT stripped here -- this projection feeds a HIGH-confidence EXACT
# match, and stripping a grade letter would auto-merge distinct siblings ('Marfil A' == 'Marfil B')
# with no review. Grade-letter handling lives in clean_variety_name (curate), with an alias/review
# gate. The slash arm only strips a thickness/render tag, not an arbitrary compound second name.
_TRAIL_TAG = re.compile(r"\s*(/\s*(\d+\s?cm|polished|honed|leather(ed)?|brushed|matt?e?)\b.*|"
                        r"\b\d+\s?cm\b|\bgrade\s+\w+)\s*$", flags=re.IGNORECASE)


def norm(value: str) -> str:
    # The engine's base projection IS the one canonical match key (core.text.match_key): casefold,
    # ascii-fold accents, fold every punctuation/separator run to one space. Kept as a thin alias so
    # the matching index and the reference vocabulary (which both key on match_key) can never diverge.
    return match_key(value)


def compact(value: str) -> str:
    # ascii_fold first (like norm) so an accented surface compacts to the SAME key as its unaccented
    # spelling -- else compact('Porriño')='porrio' (ñ dropped) != compact('Porrino')='porrino', and the
    # high-confidence exact tier fails to unite them.
    return re.sub(r"[^a-z0-9]", "", ascii_fold(value or "").casefold())


def tokenset(value: str) -> tuple[str, ...]:
    return tuple(sorted(norm(value).split()))


def deprefixed(value: str) -> str:
    text = (value or "").strip()
    text = _INV_PREFIX.sub("", text)
    text = _TRAIL_TAG.sub("", text)
    return norm(text)


def phonetic(value: str) -> tuple[str, ...]:
    tokens = norm(value).split()
    return tuple(jellyfish.metaphone(token) for token in tokens if token)


def char_similarity(a: str, b: str) -> float:
    """Cheap character similarity floor used to guard the phonetic tier so it
    does not over-merge (section 5A.2 tier 4)."""
    return jellyfish.jaro_winkler_similarity(norm(a), norm(b)) * 100.0
