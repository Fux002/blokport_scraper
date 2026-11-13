"""The ONE-TIME carry of each variety's acked alias set into ledger v18's alias_rows and codes (P2-10 C4; reviewer 2's
blocking and Mas's ruling, 13 Nov).

Ledger v18 adds alias_rows and codes (NULL: not computed yet, held from the serve) and keeps the old `aliases`, which
holds what Medusa acked. This step runs at the produce's start, after the learned-alias migration (a learned owner is
active by then) and before any scrape, build or populate, where the stores exist (config.db, the backbone). In ONE
ledger transaction it gives every NULL row's old values their owner method through the produce's own derivation
(aliases.derive.alias_sets: operator over seed over learned, a code split into codes), verifies that no row in the
whole ledger is left NULL, and drops `aliases`. A row of any kind is carried: one the produce makes again is
recomputed by its populate, and an export-only or retiring one keeps its acked set, which the alias classification
names. A value no store owns is a refusal, never a default: nothing is written, the Keys and values are named, and the
produce stops. A ledger without `aliases` is done (a fresh v18 ledger never had it): the step skips and says so, and
NULL then means only a row no produce has computed yet.

A dry run takes the same path and writes nothing, so a read-only measure (dev, prod) reports exactly what the produce
will do. No em dashes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional

from stone_pipeline.aliases import derive, payload
from stone_pipeline.aliases.surfaces import Stores, Surfaces
from stone_pipeline.core import logfmt
from stone_pipeline.core.text import alias_key

log = logfmt.get_logger("aliases.carry")

SHOWN = 20          # unownable values named in the refusal's message (the exception carries them all)
_NULL_ROWS = "SELECT COUNT(*) FROM variation WHERE alias_rows IS NULL OR codes IS NULL"


class CarryRefused(RuntimeError):
    """A carried value no store owns. `unownable` is every (Key, value), sorted; nothing was written."""

    def __init__(self, unownable: list[tuple[str, str]]):
        self.unownable = unownable
        shown = "; ".join(f"{k} {v!r}" for k, v in unownable[:SHOWN])
        more = f" and {len(unownable) - SHOWN} more" if len(unownable) > SHOWN else ""
        super().__init__(f"{len(unownable)} acked alias value(s) no store owns, so the v18 carry wrote nothing: "
                         f"{shown}{more}")


class CarryIncomplete(RuntimeError):
    """Rows were still NULL after the carry inside its transaction (a row added meanwhile): rolled back."""


@dataclass(frozen=True)
class CarryPlan:
    sets: dict[str, tuple[str, str]]               # Key -> (alias_rows JSON, codes JSON)
    unownable: tuple[tuple[str, str], ...]         # (Key, value), sorted
    values: int                                    # old values read


def carry_plan(variations: list[dict], stores: Stores, is_code: Callable[[str], bool],
               display: Callable[[str], str]) -> CarryPlan:
    """Pure: each row's ({key, type, name, aliases}) old values with their owner method from the stores, or unownable.
    A value keeps its acked spelling; its method is the one the derivation sends for its alias_key; repeats of one
    alias_key keep the least spelling (C5's rule). A code-shaped value goes to codes."""
    owned, owned_codes, _, _ = derive.alias_sets(
        ({"Key": v["key"], "Name": v["name"], "Type": v["type"]} for v in variations), stores, is_code, display)
    sets: dict[str, tuple[str, str]] = {}
    unownable: list[tuple[str, str]] = []
    values = 0
    for v in variations:
        key = v["key"]
        owner = {alias_key(piece): method for piece, method in (*owned[key], *owned_codes[key])}
        by_key: dict[str, str] = {}
        for value in json.loads(v["aliases"] or "[]"):
            values += 1
            norm = alias_key(value)
            if not norm or norm not in owner:
                unownable.append((key, value))
            elif norm not in by_key or value < by_key[norm]:
                by_key[norm] = value
        kept = [(value, owner[norm]) for norm, value in by_key.items()]
        rows, codes = payload.rows_of(Surfaces(aliases=tuple(p for p in kept if not is_code(p[0])),
                                               codes=tuple(p for p in kept if is_code(p[0]))))
        sets[key] = (json.dumps(rows, ensure_ascii=False), json.dumps(codes, ensure_ascii=False))
    return CarryPlan(sets=sets, unownable=tuple(sorted(unownable)), values=values)


def _alias_display(text: str) -> str:
    from stone_pipeline.reference.loaders import field_kinds
    return field_kinds.display(field_kinds.VARIATION_ALIAS_VALUE, text)


def run(ledger, *, stores: Optional[Stores] = None, is_code: Optional[Callable[[str], bool]] = None,
        display: Optional[Callable[[str], str]] = None, dry_run: bool = False) -> Optional[dict]:
    """Carry every NULL row's old set: {rows, values, unownable}, or None when the ledger has no `aliases`
    (done). Raises CarryRefused (nothing written) when a value has no owner. `ledger` needs only .execute and, unless
    dry_run, .conn; the stores default to the scraper's (aliases.stores.scraper_stores: seed, operator, learned; the
    export's hand rows were never sent by the scraper, so they own no carried value)."""
    from stone_pipeline.aliases import stores as alias_stores
    if "aliases" not in {r["name"] for r in ledger.execute("PRAGMA table_info(variation)")}:
        log.info("v18 alias sets already carried: no `aliases` column, nothing to do")
        return None
    variations = [dict(r) for r in ledger.execute(
        "SELECT key, type, name, aliases FROM variation WHERE alias_rows IS NULL OR codes IS NULL ORDER BY key")]
    plan = carry_plan(variations, stores if stores is not None else alias_stores.scraper_stores(),
                      is_code or alias_stores.is_code(), display or _alias_display)
    report = {"rows": len(variations), "values": plan.values, "unownable": [list(u) for u in plan.unownable]}
    if dry_run:
        return report
    if plan.unownable:
        raise CarryRefused(list(plan.unownable))
    _write(ledger.conn, plan)
    log.warning("v18: the acked alias sets moved into alias_rows and codes, `aliases` dropped (once)",
                extra={"extra_fields": report})
    return report


def _write(conn, plan: CarryPlan) -> None:
    """ONE transaction: every set written, the whole ledger checked for a NULL row, then `aliases` dropped; any error
    rolls all of it back."""
    if conn.in_transaction:
        raise RuntimeError("v18 carry: the ledger connection is inside a transaction; it must start its own")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("UPDATE variation SET alias_rows = ?, codes = ? WHERE key = ?",
                         [(rows, codes, key) for key, (rows, codes) in plan.sets.items()])
        if left := conn.execute(_NULL_ROWS).fetchone()[0]:
            raise CarryIncomplete(f"{left} variation row(s) still NULL after the carry; rolled back")
        conn.execute("ALTER TABLE variation DROP COLUMN aliases")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def run_produce_step() -> Optional[dict]:
    """The produce's call: carry on the live ledger (write-through on), the stores read where the produce reads them."""
    from stone_pipeline.ledger import writethrough
    with writethrough.open_ledger() as ledger:
        ledger.conn.commit()        # the open's own first-use seed, if any, is done work; the carry's transaction is its own
        return run(ledger)
