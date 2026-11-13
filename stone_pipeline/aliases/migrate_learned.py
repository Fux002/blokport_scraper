"""The ONE-TIME move of state/alias_writeback.csv into config.db `learned_alias` (P2-10 C2; the scraper manager's
ruling 11-12: automatic, exactly once per env, at the produce's start).

A record's variation_id is a variety's Medusa id (the phase 1 migration moved every record onto its survivor's id) or
its Key, so it resolves through the ledger's (medusa_id, key). A record becomes an ACTIVE learned alias, decided_by
'migration', when it is a learned guess (writeback.is_learned, the rule the matcher applied it under), resolves to a
LIVE Key (in the produced set, not retiring), passes the alias boundary (core.alias_problem.alias_refusal: never a
code, never the variety's own name, another same-type variety's name or a bare vocabulary word) and is the first of
its (Key, norm). Every other record is dropped and listed in review/<env>/learned_alias_migration_dropped.csv with
its reason. The marker in config.db's `migration` table is written in the same transaction as the rows, so every
later produce returns at once and never reads the csv again.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable, Iterable, Optional

from stone_pipeline.aliases.context import build_contexts
from stone_pipeline.config import decisions_store
from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.alias_problem import AliasContext, alias_refusal, code_matcher
from stone_pipeline.core.text import match_key
from stone_pipeline.state.writeback import ALIAS_WRITEBACK, is_learned, load_alias_writeback_records

log = logfmt.get_logger("aliases.migrate_learned")

MIGRATION = "learned_alias_from_writeback"
DROPPED_REVIEW = "learned_alias_migration_dropped.csv"
_DROPPED_FIELDS = ["variation_id", "alias", "method", "reason"]


def _ledger_rows(path: Path) -> list[dict]:
    """The ledger's variation rows, read-only. An absent ledger raises: there is nothing to resolve an id against."""
    if not path.exists():
        raise FileNotFoundError(f"learned-alias migration: no ledger at {path}")
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT key, type, name, medusa_id, in_full, state FROM variation")]


def plan(records: Iterable[tuple[str, str, str]], ledger_rows: list[dict], contexts: dict[str, AliasContext],
         is_code: Callable[[str], bool]) -> tuple[list[dict], list[dict]]:
    """(the active rows, the dropped records with their reason), both in a fixed order whatever the file's order: the
    records are judged sorted by (variation_id, alias, method), so the first of a (Key, norm) is always the same one."""
    key_of_id = {r["medusa_id"]: r["key"] for r in ledger_rows if r["medusa_id"]}
    live = {r["key"] for r in ledger_rows if r["in_full"] and r["state"] != "retiring"}
    active, dropped, taken = [], [], set()
    for vid, alias, method in sorted(records):
        key = key_of_id.get(vid, vid)
        if not is_learned(method):
            reason = "not_learned"
        elif key not in live:
            reason = "no_live_key"
        elif (refusal := alias_refusal(alias, contexts[key], is_code)) is not None:
            reason = refusal
        elif (key, match_key(alias)) in taken:
            reason = "duplicate"
        else:
            taken.add((key, match_key(alias)))
            active.append({"identity_key": key, "value": alias, "method": method, "source": ""})
            continue
        dropped.append({"variation_id": vid, "alias": alias, "method": method, "reason": reason})
    return active, dropped


def run_once(writeback_path: Optional[Path] = None, ledger_path: Optional[Path] = None,
             review_dir: Optional[Path] = None) -> Optional[dict]:
    """Migrate alias_writeback.csv once: {active, dropped} counts, or None when config.db already records the
    migration (the csv is then not read). An absent csv migrates nothing and still records the marker, ledger or none;
    records with no ledger to resolve them raise, unmarked."""
    if decisions_store.learned_migration_done(MIGRATION):
        return None
    from stone_pipeline.ledger import writethrough
    writeback_path = Path(writeback_path or SETTINGS.paths.state_dir / ALIAS_WRITEBACK)
    ledger_path = Path(ledger_path or writethrough.ledger_path())
    review_dir = Path(review_dir or SETTINGS.paths.review_dir)
    records = load_alias_writeback_records(writeback_path)
    # the ledger only resolves records: with none (a new environment, no csv and no ledger yet) the migration is empty
    ledger_rows = _ledger_rows(ledger_path) if records else []
    contexts = build_contexts((r["key"], r["type"], r["name"]) for r in ledger_rows
                              if r["in_full"] and r["state"] != "retiring")
    active, dropped = plan(records, ledger_rows, contexts, code_matcher(active_pack().name_code_pattern))
    # the list first: a write that fails after it leaves no marker, so the next produce redoes both
    csvio.write_dicts(review_dir / DROPPED_REVIEW, _DROPPED_FIELDS,
                      sorted(dropped, key=lambda d: (d["variation_id"], d["alias"], d["method"])), sanitize=True)
    decisions_store.record_learned_migration(MIGRATION, active)
    log.warning("alias_writeback.csv moved into learned_alias (once)", extra={"extra_fields": {
        "active": len(active), "dropped": len(dropped), "report": DROPPED_REVIEW}})
    return {"active": len(active), "dropped": len(dropped)}
