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

from stone_pipeline.core.text import ascii_fold

_PUNCT = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WS = re.compile(r"\s+")
# inventory prefixes seen on supplier exports (Z, ZB, "Z B")
_INV_PREFIX = re.compile(r"^(z\s?b|z)\s+", flags=re.IGNORECASE)
# trailing render or grade tags: a trailing slash tag, a 2cm/3cm thickness, grade letters
_TRAIL_TAG = re.compile(r"\s*(/.*|\b\d+\s?cm\b|\bgrade\s+\w+|\b[abcd]\b)\s*$", flags=re.IGNORECASE)


def norm(value: str) -> str:
    text = ascii_fold((value or "").strip()).casefold()  # fold accents so 'Porriño' == 'Porrino'
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


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
