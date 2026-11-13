"""THE derivation of a variety's alias set (P2-10 C4, D1): each Key's (value, method) aliases and codes from the alias
stores, as the produce sends them. Emit computes the payload with it. Pure: the caller passes the stores, the brand's
code test and the alias field's display rule; nothing is read or written here. No em dashes.

The rule, in ONE order: the stores' values per Key (aliases.surfaces: operator over seed over learned over admin, each
value once per alias_key, the first in that precedence and then in the store's own order), the scraper's own methods
only; every value cleaned into its pieces by the alias rule (clean_alias_list with `display`: a blob splits on its
commas, the Name and repeats dropped), then each piece a code when the code test accepts it, else an alias. A piece
whose alias_key was already taken for the Key is not sent again, so the payload carries one spelling per
(Key, alias_key) and the backend never chooses between two."""
from __future__ import annotations

from typing import Callable, Iterable

from stone_pipeline.aliases.payload import SENT_METHODS
from stone_pipeline.aliases.surfaces import OperatorAlias, Stores, Variety, surfaces_by_key
from stone_pipeline.core.text import alias_key, clean_alias_list

AliasSets = tuple[dict[str, list[tuple[str, str]]], dict[str, list[tuple[str, str]]], list[tuple[str, str]],
                  list[OperatorAlias]]


def alias_sets(rows: Iterable[dict], stores: Stores, is_code: Callable[[str], bool],
               display: Callable[[str], str]) -> AliasSets:
    """(aliases by Key, codes by Key, each [(value, method)]; the (Key, value) pieces dropped by the alias rule; the
    operator aliases that name no ONE variety) for `rows` ({Key, Name, Type})."""
    rows = list(rows)
    surfaces, unbound = surfaces_by_key([Variety(r["Key"], r["Name"], r["Type"]) for r in rows], stores, is_code)
    aliases: dict[str, list[tuple[str, str]]] = {}
    codes: dict[str, list[tuple[str, str]]] = {}
    dropped: list[tuple[str, str]] = []
    for r in rows:
        found, seen = surfaces[r["Key"]], set()
        aliases[r["Key"]], codes[r["Key"]] = [], []
        for value, method in (*found.aliases, *found.codes):
            if method not in SENT_METHODS:
                continue
            pieces, lost = clean_alias_list(r["Name"], [value], display)
            dropped.extend((r["Key"], a) for a in lost)
            for piece in pieces:
                if (key := alias_key(piece)) not in seen:
                    seen.add(key)
                    (codes if is_code(piece) else aliases)[r["Key"]].append((piece, method))
    return aliases, codes, dropped, unbound
