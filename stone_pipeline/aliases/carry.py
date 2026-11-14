"""The ONE-TIME carry of each variety's acked alias set into ledger v18's alias_rows and codes (P2-10 C4; reviewer 2's
blocking and Mas's rulings, 13 Nov).

Ledger v18 adds alias_rows and codes (NULL: not computed yet, held from the serve) and keeps the old `aliases`, which
holds what Medusa acked. This step runs at the produce's start, after the learned-alias migration (a learned owner is
active by then) and before any scrape, build or populate. Its owners are the alias classification's: ONE owner
function (aliases.cleanup.owner_of over cleanup.load_stores: the env's base Aliases and the backbone posts as seed,
the global renames as operator, the active learned rows), so the carry and the classification never disagree. For
every NULL row, each old value is:
- a code (the brand's code test accepts it): carried into codes, as the classification classes any code `code`;
- owned: carried with its owner as method, one spelling per alias_key (the least, the classification's rule);
- unowned, on any row: a REFUSAL (Mas, the owner's fix-at-source rule: no exemption). Nothing is written, the Keys
  and values are named, and the produce stops; the owner gives the value an owner in a store or removes it, then the
  carry is retried whole.
ONE ledger transaction writes every set, verifies no row in the whole ledger is left NULL, and drops `aliases`. A
ledger without `aliases` is done (a fresh v18 ledger never had it): the step skips and says so. A dry run takes the
same path, writes nothing and counts the refusals, so a read-only measure (dev, prod) reports exactly what the produce
will do. No em dashes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from stone_pipeline.aliases import cleanup, payload
from stone_pipeline.core import logfmt
from stone_pipeline.core.text import alias_key

log = logfmt.get_logger("aliases.carry")

SHOWN = 20          # refused values named in the refusal's message (the exception carries them all)
_NULL_ROWS = "SELECT COUNT(*) FROM variation WHERE alias_rows IS NULL OR codes IS NULL"


class CarryRefused(RuntimeError):
    """An acked value no store owns. `refused` is every (Key, value), sorted; nothing was written."""

    def __init__(self, refused: list[tuple[str, str]]):
        self.refused = refused
        shown = "; ".join(f"{k} {v!r}" for k, v in refused[:SHOWN])
        more = f" and {len(refused) - SHOWN} more" if len(refused) > SHOWN else ""
        super().__init__(f"{len(refused)} acked alias value(s) have no owner store, so the v18 carry wrote nothing: "
                         f"{shown}{more}")


class CarryIncomplete(RuntimeError):
    """Rows were still NULL after the carry inside its transaction (a row added meanwhile): rolled back."""


@dataclass(frozen=True)
class CarryPlan:
    sets: dict[str, tuple[str, str]]               # Key -> (alias_rows JSON, codes JSON)
    refused: tuple[tuple[str, str], ...]           # (Key, value), sorted: no store owns it
    values: int                                    # old values read


def carry_plan(variations: list[dict], stores: cleanup.Stores) -> CarryPlan:
    """Pure: each row's ({key, type, name, aliases}) old values judged by the one owner function."""
    sets: dict[str, tuple[str, str]] = {}
    refused: list[tuple[str, str]] = []
    values = 0
    for v in variations:
        kept: dict[str, tuple[str, Optional[str]]] = {}      # alias_key -> (least spelling, owner; None for a code)
        for value in json.loads(v["aliases"] or "[]"):
            values += 1
            norm = alias_key(value)
            owner = cleanup.owner_of(stores, v["key"], v["name"], v["type"], value) if norm else None
            if norm and (stores.is_code(value) or owner):
                if norm not in kept or value < kept[norm][0]:
                    kept[norm] = (value, None if stores.is_code(value) else owner)
            else:
                refused.append((v["key"], value))
        rows = payload.ordered_rows((value, owner) for value, owner in kept.values() if owner)
        codes = payload.ordered_codes(value for value, owner in kept.values() if owner is None)
        sets[v["key"]] = (json.dumps(rows, ensure_ascii=False), json.dumps(codes, ensure_ascii=False))
    return CarryPlan(sets=sets, refused=tuple(sorted(set(refused))), values=values)


def run(ledger, *, stores: Optional[cleanup.Stores] = None, dry_run: bool = False) -> Optional[dict]:
    """Carry every NULL row's old set: {rows, values, refused}, or None when the ledger has no `aliases` (done).
    Raises CarryRefused (nothing written) when any value has no owner. `ledger` needs only .execute and, unless
    dry_run, .conn; the stores default to cleanup.load_stores()."""
    if "aliases" not in {r["name"] for r in ledger.execute("PRAGMA table_info(variation)")}:
        log.info("v18 alias sets already carried: no `aliases` column, nothing to do")
        return None
    variations = [dict(r) for r in ledger.execute(
        "SELECT key, type, name, aliases FROM variation WHERE alias_rows IS NULL OR codes IS NULL ORDER BY key")]
    plan = carry_plan(variations, stores if stores is not None else cleanup.load_stores())
    report = {"rows": len(variations), "values": plan.values, "refused": [list(r) for r in plan.refused]}
    if dry_run:
        return report
    if plan.refused:
        raise CarryRefused(list(plan.refused))
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
