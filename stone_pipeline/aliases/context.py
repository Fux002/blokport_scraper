"""The alias boundary's context (core.alias_problem.AliasContext), built the scraper's way (the
two runtimes share the checks and differ only in how they build the context):
  * type_words: the scraper's clear single-word type words (adapters.tokens), match_key-keyed;
  * vocabulary_keys: match_key of every live (not archived, not internal) type, colour, finish and quality name;
  * same_type_name_keys: match_key of every name of the given varieties' type. A variety's own name is in its own
    type's set, which is harmless: own_name is checked before same_type_name.
Each set is built once per call and shared by every variety of a type.
"""

from __future__ import annotations

from typing import Iterable

from stone_pipeline.adapters.tokens import _clear_type_words, known_values
from stone_pipeline.core.alias_problem import AliasContext
from stone_pipeline.core.text import match_key
from stone_pipeline.matching import projections as proj

# The vocabulary an alias may not be a bare word of: stone-type, colour, finish and quality.
VOCABULARY_CATEGORIES = ("type", "color", "finish", "quality")


def vocabulary_keys() -> frozenset[str]:
    """match_key of every LIVE name in VOCABULARY_CATEGORIES: an archived or internal value is no longer vocabulary."""
    from stone_pipeline.reference.loaders import restricted_values
    restricted = restricted_values()
    return frozenset(match_key(value) for category in VOCABULARY_CATEGORIES for value in known_values(category)
                     if proj.norm(value) not in restricted.get(category, set()))


def build_contexts(varieties: Iterable[tuple[str, str, str]]) -> dict[str, AliasContext]:
    """{identity Key: its AliasContext} for every (Key, type name, variety name)."""
    varieties = list(varieties)
    type_words, vocabulary = _clear_type_words(), vocabulary_keys()
    names_by_type: dict[str, set[str]] = {}
    for _key, stone_type, name in varieties:
        names_by_type.setdefault(proj.norm(stone_type), set()).add(match_key(name))
    same_type = {t: frozenset(names) for t, names in names_by_type.items()}
    return {key: AliasContext(own_name=name, type_words=type_words, vocabulary_keys=vocabulary,
                              same_type_name_keys=same_type[proj.norm(stone_type)])
            for key, stone_type, name in varieties}
