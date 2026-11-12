"""An alias may never equal the name of ANOTHER variety of the SAME stone type (Mas, 11-11): compared
case-insensitively after the same name cleaning (adapters.tokens.clean_variety under the type). Such an alias binds a
listing of one stone to a different stone, and nothing merges by name without the owner's word. A variety is one
identity (type, cleaned name): its own copies in other categories, and an alias that only repeats its own name, are not
collisions. The same name under ANOTHER type is a different stone that matching keeps apart (type first): legitimate
(Mas, 11-11).

`find` lists every collision; `check` fails loud with all of them, naming each alias, its variety and the variety it
collides with. No em dashes."""
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


class AliasCollision(ValueError):
    """Aliases equal to another variety's name; the message lists every one."""

    def __init__(self, collisions: list[Collision]):
        self.collisions = collisions
        super().__init__(f"{len(collisions)} alias(es) equal another variety's name:\n"
                         + "\n".join(str(c) for c in collisions))


def _clean(text: str, stone_type: str) -> str:
    return clean_variety(text, stone_type).casefold()


def find(varieties: Iterable[Variety]) -> list[Collision]:
    """Every alias whose cleaned form is the cleaned name of a different identity of the same type. Sorted, each
    (alias, carrying identity, other identity) once."""
    rows = list(varieties)
    identity = {}                      # cleaned name -> {(type, cleaned name): (type, name, key)}
    for v in rows:
        cleaned = _clean(v.name, v.stone_type)
        identity.setdefault(cleaned, {}).setdefault((v.stone_type.casefold(), cleaned), (v.stone_type, v.name, v.key))
    out: dict[tuple, Collision] = {}
    for v in rows:
        own = (v.stone_type.casefold(), _clean(v.name, v.stone_type))
        for alias in v.aliases:
            for other_id, (o_type, o_name, o_key) in identity.get(_clean(alias, v.stone_type), {}).items():
                if other_id == own or other_id[0] != own[0]:      # itself, or another type's stone
                    continue
                c = Collision(alias, (v.stone_type, v.name), (o_type, o_name), v.key, o_key)
                out.setdefault((alias.casefold(), own, other_id), c)
    return sorted(out.values())


def check(varieties: Iterable[Variety]) -> None:
    """Fail loud on any collision, listing all of them."""
    collisions = find(varieties)
    if collisions:
        raise AliasCollision(collisions)
