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

The migration owns its input (the scraper manager's ruling 11-12): it reads the csv from its phase 1 key on S3
(writeback_key, which phase1/migrate_run writes from the pre-phase-1 key), never the task's disk, which only a
best-effort boot restore filled. The key's absence is the one thing that reads as "nothing to migrate" (a new
environment), and only while the pre-phase-1 key is absent too; any other S3 error raises and leaves no marker, so
the next produce retries instead of recording an empty move.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Callable, Iterable, Optional

from stone_pipeline.aliases.context import build_contexts
from stone_pipeline.config import decisions_store
from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import ENV_NAME, S3_BUCKET, SETTINGS
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.alias_problem import AliasContext, alias_refusal, code_matcher
from stone_pipeline.core.csvio import atomic_write
from stone_pipeline.core.text import match_key
from stone_pipeline.io import storage
from stone_pipeline.ledger import snapshot
from stone_pipeline.state.writeback import is_learned

log = logfmt.get_logger("aliases.migrate_learned")

MIGRATION = "learned_alias_from_writeback"
DROPPED_REVIEW = "learned_alias_migration_dropped.csv"
_DROPPED_FIELDS = ["variation_id", "alias", "method", "reason"]
ALIAS_WRITEBACK = "alias_writeback.csv"
_FIELDS = ["variation_id", "alias", "method"]


def writeback_key(env: str = ENV_NAME) -> str:
    """The csv's S3 key: the durable-state prefix (ENV_NAME) and phase 1's store generation."""
    stem, suffix = ALIAS_WRITEBACK.rsplit(".", 1)
    return f"{env}/scraper/state/{stem}{snapshot.STORE_GENERATION}.{suffix}"


def _parse(handle) -> list[tuple[str, str, str]]:
    """The csv's records (variation_id, alias, method). Tolerates a legacy two-column file (method reads as "")."""
    out: list[tuple[str, str, str]] = []
    for record in csv.DictReader(handle):
        vid = (record.get("variation_id") or "").strip()
        alias = (record.get("alias") or "").strip()
        method = (record.get("method") or "").strip()
        if vid and alias:
            out.append((vid, alias, method))
    return out


def load_alias_writeback_records(path: Path) -> list[tuple[str, str, str]]:
    """A local copy's records (phase1/migrate_run reads the downloaded pre-phase-1 object); [] when it is absent."""
    path = Path(path)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return _parse(handle)


def write_alias_writeback_records(path: Path, records: list[tuple[str, str, str]]) -> None:
    """phase1/migrate_run's writer of the .v2 copy. Atomic temp-file + os.replace: a whole-file rewrite must never
    leave a truncated file."""
    def _write(handle) -> None:
        writer = csv.writer(handle)
        writer.writerow(_FIELDS)
        for vid, alias, method in records:
            writer.writerow([vid, alias, method])
    atomic_write(path, _write)


def _s3():
    return storage.aws_client("s3", config=storage.BOUNDED)


def stored_records(env: str = ENV_NAME) -> list[tuple[str, str, str]]:
    """The records at writeback_key. Absent: [] on a new environment, MigrationPending while the pre-phase-1 key still
    holds the csv (phase 1 has not run here). Any other error raises: it says nothing about the csv's content."""
    key = writeback_key(env)
    try:
        body = _s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
    except Exception as exc:
        if not storage.s3_error_is_missing(exc):
            raise
        snapshot._refuse_an_unmigrated_store(key)
        log.info("no alias write-back to migrate", extra={"extra_fields": {"key": key}})
        return []
    return _parse(io.StringIO(body.decode("utf-8-sig"), newline=""))


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


def run_once(ledger_path: Optional[Path] = None, review_dir: Optional[Path] = None) -> Optional[dict]:
    """Migrate alias_writeback.csv once: {active, dropped} counts, or None when config.db already records the
    migration (S3 is then not read). A new environment's absent csv migrates nothing and still records the marker,
    ledger or none; an S3 error, an unmigrated stack or records with no ledger to resolve them raise, unmarked."""
    if decisions_store.learned_migration_done(MIGRATION):
        return None
    from stone_pipeline.ledger import writethrough
    ledger_path = Path(ledger_path or writethrough.ledger_path())
    review_dir = Path(review_dir or SETTINGS.paths.review_dir)
    records = stored_records()
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
