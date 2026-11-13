"""Each variety's surfaces (its aliases and its codes) from the stores that own them (P2-10-SCRAPER-PLAN.md section 3):

- `seed`: the variety's backbone post (`aliases`, `codes`), keyed by its Key. The backbone is the seed's ONE store of
  record; the base's Aliases column is output, never read back as a store (that loop made a learned guess a seed
  alias with no review).
- `operator`: a GLOBAL `is` statement whose stated name differs from the spelling: the spelling is that variety's alias.
  A vendor binding stays match-only (the matcher's override tier), so it is no surface here.
- `learned`: an ACTIVE `learned_alias` row (a proposal is no surface until approved), keyed by identity Key.
- `admin`: the backend export's hand rows, AdminAliases and AdminCodes (P2-7 E1).

ONE boundary for every store: a value the brand's code pattern accepts is a CODE, never an alias (C1's alias_refusal
order). A value sits once per variety, under the first store that holds it in the order operator, seed, learned, admin
(the classification's precedence, operator over seed over learned; the hand rows last, since the backend already holds
them). The matcher, alias_target and the payload all read `surfaces_by_key`, so "is this a known alias" has one answer.
Pure: no file, store or network read here (the loaders build the inputs). No em dashes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

from stone_pipeline.core.text import match_key

METHODS = ("operator", "seed", "learned", "admin")      # the precedence order, first wins


@dataclass(frozen=True)
class Surfaces:
    """One variety's surfaces, each (value, method), in METHODS order then store order."""
    aliases: tuple[tuple[str, str], ...] = ()
    codes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OperatorAlias:
    """A global `is` statement with a stated name other than the spelling: `spelling` is an alias of the variety
    (`name`, `stone_type`). An empty stone_type names the variety by name alone."""
    name: str
    stone_type: str
    spelling: str


@dataclass(frozen=True)
class Stores:
    """The four stores' values, read by the loaders. seed and admin: Key -> (aliases, codes); learned: (identity Key,
    value) of ACTIVE rows."""
    seed: Mapping[str, tuple[Sequence[str], Sequence[str]]] = field(default_factory=dict)
    operator: Sequence[OperatorAlias] = ()
    learned: Sequence[tuple[str, str]] = ()
    admin: Mapping[str, tuple[Sequence[str], Sequence[str]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Variety:
    key: str
    name: str
    stone_type: str


def surfaces_by_key(varieties: Iterable[Variety], stores: Stores,
                    is_code: Callable[[str], bool]) -> tuple[dict[str, Surfaces], list[OperatorAlias]]:
    """Every variety's Surfaces by Key, and the operator aliases that name no ONE variety (no variety of that name and
    type, or an untyped name two types share): those are skipped, never bound to a guess, and the caller lists them."""
    rows = list(varieties)
    values: dict[str, dict[str, list[tuple[str, str]]]] = {v.key: {m: [] for m in METHODS} for v in rows}

    def put(key: str, method: str, raw: Iterable[str]) -> None:
        if key in values:
            values[key][method].extend((v.strip(), method) for v in raw if v and v.strip())

    for key, (aliases, codes) in stores.seed.items():
        put(key, "seed", [*aliases, *codes])
    for key, value in stores.learned:
        put(key, "learned", [value])
    for key, (aliases, codes) in stores.admin.items():
        put(key, "admin", [*aliases, *codes])
    by_name: dict[str, list[Variety]] = {}
    for v in rows:
        by_name.setdefault(match_key(v.name), []).append(v)
    unbound: list[OperatorAlias] = []
    for op in stores.operator:
        hits = [v for v in by_name.get(match_key(op.name), [])
                if not op.stone_type or match_key(v.stone_type) == match_key(op.stone_type)]
        if len(hits) == 1:
            put(hits[0].key, "operator", [op.spelling])
        else:
            unbound.append(op)
    return {key: _split(per_method, is_code) for key, per_method in values.items()}, unbound


def _split(per_method: dict[str, list[tuple[str, str]]], is_code: Callable[[str], bool]) -> Surfaces:
    """The values in METHODS order, once per match_key, each a code when the pattern accepts it, else an alias."""
    seen: set[str] = set()
    aliases: list[tuple[str, str]] = []
    codes: list[tuple[str, str]] = []
    for method in METHODS:
        for value, _ in per_method[method]:
            if not (key := match_key(value)) or key in seen:
                continue
            seen.add(key)
            (codes if is_code(value) else aliases).append((value, method))
    return Surfaces(tuple(aliases), tuple(codes))
