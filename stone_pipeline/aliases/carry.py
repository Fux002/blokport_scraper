"""The ONE-TIME carry of each variety's acked alias set into ledger v18's alias_rows and codes (P2-10 C4; reviewer 2's
blocking and Mas's final ruling, 13 Nov).

Ledger v18 adds alias_rows and codes (NULL: not computed yet, held from the serve) and keeps the old `aliases`, which
holds what Medusa acked. This step runs at the produce's start, after the learned-alias migration (a learned owner is
active by then) and before any scrape, build or populate. Its owners are the alias classification's: ONE owner
function (aliases.cleanup.owner_of over cleanup.load_stores: the env's base Aliases and the backbone posts as seed,
the global renames as operator, the active learned rows), so the carry and the classification never disagree. For
every NULL row, each old value is:
- a code (the brand's code test accepts it): carried into codes, as the classification classes any code `code`;
- owned: carried with its owner as method, one spelling per alias_key (the least, the classification's rule);
- unowned on a row OUTSIDE the produced set (in_full=0, or retiring): a REFUSAL. Nothing is written, the Keys and
  values are named, and the produce stops; the owner names an owner or a removal, then the carry is retried whole;
- unowned on a row IN the produced set: SUPERSEDED. It is not carried; the produce re-serves that row's new whole
  set, so Medusa replaces it. It is recorded in variation_alias_superseded and listed in
  review/<env>/alias_carry_superseded.csv, and the classification names it `removed` if the produce leaves the row
  out instead (in_full=0, no tombstone), since Medusa then still holds it.
The list is written first, then ONE ledger transaction writes every set and superseded value, verifies no row in the
whole ledger is left NULL, and drops `aliases`. A ledger without `aliases` is done (a fresh v18 ledger never had
it): the step skips and says so. A dry run takes the same path, writes nothing and counts both sets, so a read-only
measure (dev, prod) reports exactly what the produce will do. No em dashes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from stone_pipeline.aliases import cleanup, payload
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.text import alias_key

log = logfmt.get_logger("aliases.carry")

SHOWN = 20          # refused values named in the refusal's message (the exception carries them all)
SUPERSEDED_REVIEW = "alias_carry_superseded.csv"
_SUPERSEDED_FIELDS = ["Key", "value"]
_NULL_ROWS = "SELECT COUNT(*) FROM variation WHERE alias_rows IS NULL OR codes IS NULL"
_RETIRING = "retiring"


class CarryRefused(RuntimeError):
    """An unowned value on a row outside the produced set. `refused` is every (Key, value), sorted; nothing was
    written."""

    def __init__(self, refused: list[tuple[str, str]]):
        self.refused = refused
        shown = "; ".join(f"{k} {v!r}" for k, v in refused[:SHOWN])
        more = f" and {len(refused) - SHOWN} more" if len(refused) > SHOWN else ""
        super().__init__(f"{len(refused)} acked alias value(s) on rows outside the produced set have no owner store, "
                         f"so the v18 carry wrote nothing: {shown}{more}")


class CarryIncomplete(RuntimeError):
    """Rows were still NULL after the carry inside its transaction (a row added meanwhile): rolled back."""


@dataclass(frozen=True)
class CarryPlan:
    sets: dict[str, tuple[str, str]]               # Key -> (alias_rows JSON, codes JSON)
    refused: tuple[tuple[str, str], ...]           # (Key, value), sorted: unowned, row outside the produced set
    superseded: tuple[tuple[str, str], ...]        # (Key, value), sorted: unowned, row in the produced set
    values: int                                    # old values read


def carry_plan(variations: list[dict], stores: cleanup.Stores) -> CarryPlan:
    """Pure: each row's ({key, type, name, aliases, in_full, state}) old values judged by the one owner function."""
    sets: dict[str, tuple[str, str]] = {}
    refused: list[tuple[str, str]] = []
    superseded: list[tuple[str, str]] = []
    values = 0
    for v in variations:
        in_set = bool(v["in_full"]) and v["state"] != _RETIRING
        kept: dict[str, tuple[str, Optional[str]]] = {}      # alias_key -> (least spelling, owner; None for a code)
        for value in json.loads(v["aliases"] or "[]"):
            values += 1
            norm = alias_key(value)
            owner = cleanup.owner_of(stores, v["key"], v["name"], v["type"], value) if norm else None
            if norm and (stores.is_code(value) or owner):
                if norm not in kept or value < kept[norm][0]:
                    kept[norm] = (value, None if stores.is_code(value) else owner)
            else:
                (superseded if in_set else refused).append((v["key"], value))
        rows = payload.ordered_rows((value, owner) for value, owner in kept.values() if owner)
        codes = payload.ordered_codes(value for value, owner in kept.values() if owner is None)
        sets[v["key"]] = (json.dumps(rows, ensure_ascii=False), json.dumps(codes, ensure_ascii=False))
    return CarryPlan(sets=sets, refused=tuple(sorted(set(refused))), superseded=tuple(sorted(set(superseded))),
                     values=values)


def run(ledger, *, stores: Optional[cleanup.Stores] = None, review_dir: Optional[Path] = None,
        dry_run: bool = False) -> Optional[dict]:
    """Carry every NULL row's old set: {rows, values, refused, superseded}, or None when the ledger has no `aliases`
    (done). Raises CarryRefused (nothing written) when a row outside the produced set holds an unowned value.
    `ledger` needs only .execute and, unless dry_run, .conn; the stores default to cleanup.load_stores()."""
    if "aliases" not in {r["name"] for r in ledger.execute("PRAGMA table_info(variation)")}:
        log.info("v18 alias sets already carried: no `aliases` column, nothing to do")
        return None
    variations = [dict(r) for r in ledger.execute(
        "SELECT key, type, name, aliases, in_full, state FROM variation WHERE alias_rows IS NULL OR codes IS NULL "
        "ORDER BY key")]
    plan = carry_plan(variations, stores if stores is not None else cleanup.load_stores())
    report = {"rows": len(variations), "values": plan.values, "refused": [list(r) for r in plan.refused],
              "superseded": [list(s) for s in plan.superseded]}
    if dry_run:
        return report
    if plan.refused:
        raise CarryRefused(list(plan.refused))
    _list_superseded(Path(review_dir) if review_dir is not None else _review_dir(), plan.superseded)
    _write(ledger.conn, plan)
    log.warning("v18: the acked alias sets moved into alias_rows and codes, `aliases` dropped (once)",
                extra={"extra_fields": {**report, "superseded": len(plan.superseded),
                                        "report": SUPERSEDED_REVIEW}})
    return report


def _review_dir() -> Path:
    from stone_pipeline.config.settings import SETTINGS
    return SETTINGS.paths.review_dir


def _list_superseded(review_dir: Path, superseded: tuple[tuple[str, str], ...]) -> None:
    """The operator's list, written before the transaction: a failed write after it leaves `aliases`, so the next
    produce redoes both."""
    csvio.write_dicts(review_dir / SUPERSEDED_REVIEW, _SUPERSEDED_FIELDS,
                      [{"Key": k, "value": v} for k, v in superseded], sanitize=True)


def _write(conn, plan: CarryPlan) -> None:
    """ONE transaction: every set and superseded value written, the whole ledger checked for a NULL row, then `aliases`
    dropped; any error rolls all of it back."""
    if conn.in_transaction:
        raise RuntimeError("v18 carry: the ledger connection is inside a transaction; it must start its own")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("UPDATE variation SET alias_rows = ?, codes = ? WHERE key = ?",
                         [(rows, codes, key) for key, (rows, codes) in plan.sets.items()])
        conn.executemany("INSERT INTO variation_alias_superseded (key, value) VALUES (?, ?)", plan.superseded)
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
