"""The one boundary check for an alias value: the scraper twin of the backend's aliasProblem and codeMatcher. The
phase 2 cleaner pre-classifies every value with it before the payload states it (a code goes in `codes`, a refused
alias never reaches `alias_rows`), so the backend never holds an item for a disagreement.

PAIRED with the backend through tests/fixtures/alias_problem_vectors.json, byte-identical to the backend's
src/model/catalog/__tests__/alias-problem.vectors.json: a change to either side is a paired PR. A value's key is
core.text.alias_key, the backend aliasKey's paired twin (match_key for an all-Latin value; the `alias_key` column
of tests/fixtures/name_key_vectors.json); names (the own name, the context sets) key by match_key. Both are pure:
the same input always gives the same answer, and neither throws. No em dashes (design principle 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Optional

from stone_pipeline.core.text import alias_key, keeps_script, match_key

# aliasProblem's codes, in the order it checks them (the first that applies wins). A contract with the backend's
# AliasProblem type, not a tunable.
PROBLEMS = ("delimiter", "empty", "own_name", "own_name_type_word", "same_type_name", "bare_vocabulary_word",
            "too_short")
# A RAW value holding one of these is refused: they separate list items in the CSV and export cells. A comma is part
# of a name, never a delimiter.
_DELIMITERS = ("|", "\n", "\r")
# A match_key (all-Latin value) of at most this many characters is refused (too_short).
_TOO_SHORT_MAX = 2


@dataclass(frozen=True)
class AliasContext:
    """What an alias is judged against: the variety's own name (raw), and three sets of keys already through
    match_key: the single-word type names, every vocabulary value, and the names of the other varieties of the same
    type. The caller builds them; the check only reads them."""
    own_name: str
    type_words: frozenset[str]
    vocabulary_keys: frozenset[str]
    same_type_name_keys: frozenset[str]


def alias_problem(value: str, context: AliasContext) -> Optional[str]:
    """The FIRST of PROBLEMS that applies to `value` as an alias of the variety `context` describes, or None for a
    good alias. `delimiter` reads the raw value; every other check reads its key."""
    if any(d in value for d in _DELIMITERS):
        return "delimiter"
    key = alias_key(value)
    if not key:
        return "empty"
    own = match_key(context.own_name)
    if own and key == own:
        return "own_name"
    # a key's words are joined by single spaces, so the tail after "<own> " is the one candidate type word
    if own and key.startswith(f"{own} ") and key[len(own) + 1:] in context.type_words:
        return "own_name_type_word"
    if key in context.same_type_name_keys:
        return "same_type_name"
    if key in context.vocabulary_keys:
        return "bare_vocabulary_word"
    # a script-preserving key is never too short: a CJK character carries a word (the backend likewise)
    if len(key) <= _TOO_SHORT_MAX and not keeps_script(value):
        return "too_short"
    return None


def alias_refusal(value: str, context: AliasContext, is_code: Callable[[str], bool]) -> Optional[str]:
    """THE ALIAS JUDGEMENT, in one place (the backend's aliasRefusal): a value the brand's code pattern accepts is a
    code, never an alias, so `is_code` is tested first; otherwise alias_problem in the variety's context. The cleaner
    judges every alias value here, so the order is stated once."""
    return "is_code" if is_code(value) else alias_problem(value, context)


@lru_cache(maxsize=None)
def code_matcher(pattern: Optional[str]) -> Callable[[str], bool]:
    """The test for a CODE under a brand's pattern (its pack's name_code_pattern): a value is a code when its
    alias_key has at least one token and EVERY token fully matches the pattern, case-insensitively (the key's tokens
    are lower case), so a non-Latin token is never dropped into a code. A None pattern means the brand has no codes, so nothing is one. An empty pattern raises (it matches no
    token, so it would quietly mean "no codes": a brand with none states None), and so does an invalid one
    (re.error). Compiled once per pattern."""
    if pattern is None:
        return lambda value: False
    if pattern == "":
        raise ValueError("an empty code pattern matches no token; a brand with no codes states None")
    compiled = re.compile(pattern, re.IGNORECASE)

    def is_code(value: str) -> bool:
        tokens = alias_key(value).split()
        return bool(tokens) and all(compiled.fullmatch(token) for token in tokens)
    return is_code
