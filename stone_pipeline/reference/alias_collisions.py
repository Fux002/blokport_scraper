"""An alias may never equal the name of ANOTHER variety of the SAME stone type: compared case-insensitively after
the same name cleaning (adapters.tokens.clean_variety under the type). Such an alias binds a listing of one stone to a
different stone, and nothing merges by name without the owner's word. A variety is one identity (type, cleaned name):
every spelling under its one Key, and an alias that only repeats its own name, are not collisions. The same name under
ANOTHER type is a different stone that matching keeps apart (type first): legitimate.

`find` lists every collision, each naming the alias, its variety and the variety it collides with. CI holds the
committed vocabulary at 0 (tests/test_alias_collisions.py); at emit a collision with a live variety is dropped and
listed, never a stop (stages/emit_catalog.py). No em dashes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from stone_pipeline.adapters.tokens import clean_variety


@dataclass(frozen=True)
class Variety:
    """One variety as a committed file carries it: its Key (or a backbone post's label), type, name and aliases."""
    key: str
    stone_type: str
    name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True, order=True)
class Collision:
    alias: str
    variety: tuple[str, str]           # (type, name) that carries the alias
    collides_with: tuple[str, str]     # (type, name) of the other variety the alias names
    key: str                           # a Key of the carrying variety
    other_key: str                     # a Key of the other variety

    def __str__(self) -> str:
        return (f"alias {self.alias!r} of {self.variety[0]} {self.variety[1]!r} ({self.key}) is the name of "
                f"{self.collides_with[0]} {self.collides_with[1]!r} ({self.other_key})")


def _clean(text: str, stone_type: str) -> str:
    return clean_variety(text, stone_type).casefold()


def find(varieties: Iterable[Variety]) -> list[Collision]:
    """Every alias whose cleaned form is the cleaned name of a different variety of the same type. Sorted, each
    (alias, carrying identity, other identity) once. One variety is every (type, cleaned name) that appears under one
    Key (Scraper M2): a backbone post spelling its own variety 'G562' under 'G562 Maple Red''s Key is that variety's
    own name, never another variety."""
    rows = list(varieties)
    parent: dict = {}

    def root(node):
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def node_of(v):
        return (v.stone_type.casefold(), _clean(v.name, v.stone_type))

    first_of_key: dict = {}
    for v in rows:
        n = node_of(v)
        root(n)
        if v.key in first_of_key:
            parent[root(n)] = root(first_of_key[v.key])
        else:
            first_of_key[v.key] = n
    identity = {}                      # cleaned name -> {(type, cleaned name): (type, name, key)}
    for v in rows:
        cleaned = _clean(v.name, v.stone_type)
        identity.setdefault(cleaned, {}).setdefault((v.stone_type.casefold(), cleaned), (v.stone_type, v.name, v.key))
    out: dict[tuple, Collision] = {}
    for v in rows:
        own = node_of(v)
        for alias in v.aliases:
            for other_id, (o_type, o_name, o_key) in identity.get(_clean(alias, v.stone_type), {}).items():
                if other_id[0] != own[0] or root(other_id) == root(own):   # another type's stone, or itself
                    continue
                c = Collision(alias, (v.stone_type, v.name), (o_type, o_name), v.key, o_key)
                out.setdefault((alias.casefold(), own, other_id), c)
    return sorted(out.values())
