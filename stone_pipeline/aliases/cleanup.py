"""The alias classification file (P2-10 C5; plan P2-10-SCRAPER-PLAN.md section 5; table 249 rev 3 "Definitions"):
`python -m stone_pipeline.aliases.cleanup`, and `POST /config/v1/aliases/classification`
(lifecycle.alias_classification).

The backend's phase 2 fill reads it to give every scraper alias row on Medusa its owner. It names every served Key and
every pending variation tombstone with its CURRENT ACKED set (the ledger keeps no history; from ledger v18, P2-10 C4,
the served set is a row's alias_rows values and its codes), one row per
(Key, alias_key(Value)), each value in ONE class, the first that applies:
- `code`: the brand's code pattern accepts it (C1's code test, inside alias_refusal);
- `operator`: a GLOBAL `is` statement renamed onto the variety (vendor bindings stay match-only);
- `removed`: a seed or learned value C1's alias refusal fails (the cleanup drops it; an operator's row is never
  changed), or a value the v18 carry superseded on a row the produce has since left out (in_full=0);
- `seed`: the env's base `Aliases` or a phase 1 backbone post's `aliases`;
- `learned`: an ACTIVE learned_alias row (C2);
and a Key holding no value is the one row `Key,,none`.

It is written only when every variation is synced (or retiring with its pending tombstone) and its served set is
computed (alias_rows and codes not NULL): otherwise it refuses, naming the Keys. A served value no store owns, a value with an empty key, and a pending tombstone whose row is gone
refuse too, so the file is whole or absent. Deterministic: the same ledger and stores give the same bytes (sorted
everywhere, no set order reaches the output), and a run writes nothing to the ledger or config.db.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from stone_pipeline.aliases import open_items
from stone_pipeline.aliases.context import build_contexts
from stone_pipeline.core import logfmt
from stone_pipeline.core.alias_problem import AliasContext, alias_refusal, code_matcher
from stone_pipeline.core.text import alias_key, match_key

log = logfmt.get_logger("alias_cleanup")

# --- config ----------------------------------------------------------------------------------------------------------
FILE_NAME = "alias_classification.csv"
DIFFERENCES_FILE = "alias_cleanup_differences.csv"
COLUMNS = ("Key", "Value", "Class")
DIFFERENCE_COLUMNS = ("Key", "Value", "Class", "Reason")
MANIFEST_FIELD = "alias_classification"
# The manifest field's fixed statements (table 249 rev 3: the backend's G1 compares them exactly).
BASIS = "all variations synced"
KEYS_TEXT = "every served Key and pending tombstone"
VALUES_TEXT = "current acked set"
SYNCED = "synced"
RETIRING = "retiring"                      # held until its pending variation tombstone is acked: named by its last set
KEYS_SHOWN = 20                            # how many Keys a refusal's message names (the full list is in `keys`)
NO_OWNER = "no owner store"
EMPTY_KEY = "empty key"
ORPHAN_TOMBSTONE = "pending tombstone with no ledger row (no last set to name)"
# A value the v18 carry superseded (no store owned it; its row was in the produced set): Medusa still holds it once
# the produce left that row out (in_full=0), so the file names it removed (aliases.carry, Mas 13 Nov).
SUPERSEDED = "superseded at the v18 carry (no owner store), its row left the produced set"
# The classes whose values are aliases served as such (not a code, not removed): a digit in one is an open item.
ALIAS_CLASSES = ("seed", "operator", "learned")
ADMIN_ALIASES_COLUMN = "AdminAliases"      # the live export's admin rows (P2-7's E1), read by header


class NotAllSynced(RuntimeError):
    """A variation is not synced: the file would not describe Medusa. `keys` names every one."""
    STATE, NEXT = "not synced", "run a draining Publish first"

    def __init__(self, keys: list[str]):
        self.keys = keys
        shown = ", ".join(keys[:KEYS_SHOWN]) + (f" and {len(keys) - KEYS_SHOWN} more" if len(keys) > KEYS_SHOWN else "")
        super().__init__(f"{len(keys)} variation(s) {self.STATE}: {shown}; {self.NEXT}")


class NotAllComputed(NotAllSynced):
    """A variation's served set is not computed yet (ledger v18: alias_rows or codes NULL until a produce computes
    them): the file would not describe Medusa either, so it refuses as an unsynced row does (the same 409 naming the
    Keys). `keys` names every one."""
    STATE, NEXT = "with no computed alias_rows/codes", "run a produce first"


class Unclassifiable(RuntimeError):
    """A served value or a Key the file cannot class. `refused` is [(Key, Value, reason)]."""

    def __init__(self, refused: list[tuple[str, str, str]]):
        self.refused = refused
        shown = "; ".join(f"{k} {v!r}: {why}" for k, v, why in refused[:KEYS_SHOWN])
        super().__init__(f"{len(refused)} value(s) cannot be classified: {shown}")


@dataclass(frozen=True)
class Stores:
    """The alias owners, keyed for the lookups: seed and learned by Key, operator by (match_key(name), match_key(type)),
    every value as its alias_key; plus the brand's code test."""
    seed: dict[str, frozenset[str]]
    operator: dict[tuple[str, str], frozenset[str]]
    learned: dict[str, frozenset[str]]
    is_code: Callable[[str], bool]


@dataclass(frozen=True)
class Classification:
    rows: tuple[tuple[str, str, str], ...]             # (Key, Value, Class), sorted by Key then alias_key(Value)
    variations: int                                    # synced rows named
    tombstoned: int                                    # retiring rows named (their pending tombstone)
    differences: tuple[tuple[str, str, str, str], ...]  # (Key, Value, 'removed' | 'refused', reason), same order


# --- the classification ----------------------------------------------------------------------------------------------
def unsynced_keys(ledger) -> list[str]:
    """Every variation not synced (and not retiring), or abandoned, sorted."""
    return [r["key"] for r in ledger.execute(
        "SELECT key FROM variation WHERE state NOT IN (?, ?) OR abandoned_at IS NOT NULL ORDER BY key",
        (SYNCED, RETIRING))]


def uncomputed_keys(ledger) -> list[str]:
    """Every variation whose served set is not computed yet (alias_rows or codes NULL), sorted."""
    return [r["key"] for r in ledger.execute(
        "SELECT key FROM variation WHERE alias_rows IS NULL OR codes IS NULL ORDER BY key")]


def classify(ledger, stores: Stores) -> Classification:
    """The file's rows from the ledger's variation rows and pending variation tombstones. Raises NotAllSynced (or
    NotAllComputed) before reading any value; every other problem is a `refused` difference (require_whole raises on
    them)."""
    if keys := unsynced_keys(ledger):
        raise NotAllSynced(keys)
    if keys := uncomputed_keys(ledger):
        raise NotAllComputed(keys)
    variations = [dict(r) for r in ledger.execute(
        "SELECT key, type, name, alias_rows, codes, state, in_full FROM variation")]
    contexts = alias_contexts(variations)
    rows: list[tuple[str, str, str]] = []
    differences: list[tuple[str, str, str, str]] = []
    for v in variations:
        _classify_variety(v, stores, contexts[v["key"]], rows, differences)
    out_of_set = {v["key"] for v in variations if not v["in_full"]}
    named = {(row[0], alias_key(row[1])) for row in (*rows, *differences)}
    for r in ledger.execute("SELECT key, value FROM variation_alias_superseded ORDER BY key, value"):
        # a value the Key serves again (a store came to own it before the row left the set) is classed by its set
        if r["key"] in out_of_set and (r["key"], alias_key(r["value"])) not in named:
            rows.append((r["key"], r["value"], "removed"))
            differences.append((r["key"], r["value"], "removed", SUPERSEDED))
    held = {v["key"] for v in variations}
    for r in ledger.execute("SELECT external_id FROM removed WHERE kind = 'variation' ORDER BY external_id"):
        if r["external_id"] not in held:
            differences.append((r["external_id"], "", "refused", ORPHAN_TOMBSTONE))
    return Classification(rows=tuple(sorted(rows, key=_order)),
                          variations=sum(v["state"] == SYNCED for v in variations),
                          tombstoned=sum(v["state"] == RETIRING for v in variations),
                          differences=tuple(sorted(differences, key=_order)))


def _order(row: tuple) -> tuple:
    """Sorted by Key, then alias_key(Value), then the Value itself (two spellings never share a row, so this is
    total)."""
    return row[0], alias_key(row[1]), row[1]


def alias_contexts(variations: list[dict]) -> dict[str, AliasContext]:
    """{Key: its alias-check context}, built by C2's one builder (aliases.context.build_contexts) over the varieties
    C2's migration judges against: the live ones (in the full set, not retiring). A Key outside them (retiring, or an
    export-only row) is judged against every variety, its own type's names included."""
    def triple(v: dict) -> tuple[str, str, str]:
        return v["key"], v["type"], v["name"]
    live = build_contexts(triple(v) for v in variations if v["in_full"] and v["state"] != RETIRING)
    rest = build_contexts(triple(v) for v in variations)
    return {v["key"]: live.get(v["key"]) or rest[v["key"]] for v in variations}


def seed_refusal(value: str, context: AliasContext, is_code: Callable[[str], bool]) -> Optional[str]:
    """THE `removed` judgement for a seed or learned value, shared with the seed rewrite (one place decides): C1's
    alias refusal, except that a code is never a refusal (it moves to `codes`). None for a good alias or a code."""
    refusal = alias_refusal(value, context, is_code)
    return None if refusal == "is_code" else refusal


def served_values(v: dict) -> list[str]:
    """The values Medusa holds for a variety from the scraper (ledger v18): its alias_rows values, then its codes.
    Their method does not class them: the stores do (operator over seed over learned, a code whoever owns it)."""
    return [row["value"] for row in json.loads(v["alias_rows"])] + list(json.loads(v["codes"]))


def owner_of(stores: Stores, key: str, name: str, stone_type: str, value: str) -> Optional[str]:
    """THE owner of one value on one variety, the first store that holds its alias_key: 'operator' (a global rename onto
    the variety's (name, type), or onto its name with no type), then 'seed', then 'learned'; None when no store holds
    it. The classification and the v18 carry (aliases.carry) both ask here, so they never disagree."""
    norm = alias_key(value)
    name_key = match_key(name)
    operator = stores.operator
    if norm in operator.get((name_key, match_key(stone_type)), ()) or norm in operator.get((name_key, ""), ()):
        return "operator"
    if norm in stores.seed.get(key, ()):
        return "seed"
    if norm in stores.learned.get(key, ()):
        return "learned"
    return None


def _classify_variety(v: dict, stores: Stores, context: AliasContext, rows: list, differences: list) -> None:
    key = v["key"]
    values = served_values(v)
    if not values:
        rows.append((key, "", "none"))
        return
    by_norm: dict[str, list[str]] = {}
    for value in values:
        by_norm.setdefault(alias_key(value), []).append(value)
    for norm, spellings in by_norm.items():
        value = min(spellings)                          # one row per (Key, norm): the least spelling, deterministic
        if not norm:
            differences.append((key, value, "refused", EMPTY_KEY))
            continue
        owner = owner_of(stores, key, v["name"], v["type"], value)
        if stores.is_code(value):
            rows.append((key, value, "code"))
        elif owner == "operator":
            rows.append((key, value, "operator"))
        elif owner and (refusal := seed_refusal(value, context, stores.is_code)):
            rows.append((key, value, "removed"))
            differences.append((key, value, "removed", refusal))
        elif owner:
            rows.append((key, value, owner))
        else:
            differences.append((key, value, "refused", NO_OWNER))


def require_whole(classification: Classification) -> None:
    """Raise Unclassifiable when any value or Key was refused: the file is whole or absent."""
    if refused := [(k, v, why) for k, v, cls, why in classification.differences if cls == "refused"]:
        raise Unclassifiable(refused)


def manifest_field(classification: Classification, generated_at: str) -> dict:
    return {"complete": True, "basis": BASIS, "keys": KEYS_TEXT, "values": VALUES_TEXT,
            "variations": classification.variations, "tombstoned": classification.tombstoned,
            "generated_at": generated_at}


def _csv(columns, rows) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(columns)
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def csv_bytes(classification: Classification) -> bytes:
    """The file: UTF-8 with no BOM, \\n line ends, minimal quoting, sorted by Key then alias_key(Value)."""
    return _csv(COLUMNS, classification.rows)


def differences_bytes(classification: Classification) -> bytes:
    """The owner's difference list: every removed and every refused value, with why."""
    return _csv(DIFFERENCE_COLUMNS, classification.differences)


# --- the stores ------------------------------------------------------------------------------------------------------
def _seed_files() -> tuple[Path, list[Path]]:
    """The env's base (the seed's `Aliases`, grown by every produce) and the phase 1 backbone files."""
    from stone_pipeline.config.settings import SETTINGS
    return (SETTINGS.paths.variants_export_base_csv,
            [SETTINGS.paths.backbone_json, SETTINGS.paths.backbone_additions_dir / "varieties.json"])


def load_stores() -> Stores:
    """The owners as this env holds them now: the base and the backbone (seed), config.db's global renamed `is`
    statements (operator) and its ACTIVE learned rows (learned). Read-only."""
    from stone_pipeline.config import decisions_store
    from stone_pipeline.config.domain import active_pack
    from stone_pipeline.reference.loaders.backbone import read_posts
    from stone_pipeline.reference.medusa_files import read_variants_export
    base, backbones = _seed_files()
    seed: dict[str, set[str]] = {}
    for row in read_variants_export(Path(base), require_status=False, name=Path(base).name):
        seed.setdefault(row.key, set()).update(alias_key(a) for a in row.aliases)
    for path in backbones:
        if Path(path).exists():
            for post in read_posts(Path(path)):
                if post.get("key"):
                    seed.setdefault(post["key"], set()).update(alias_key(a) for a in post.get("aliases") or [])
    operator: dict[tuple[str, str], set[str]] = {}
    for s in decisions_store.statement_rows():
        display = s.get("spelling") or s["spelling_norm"]
        if (s["source"] or "") or s["verdict"] != "is" or not s.get("name"):
            continue                                    # a vendor binding is match-only; a reject names no alias
        if match_key(s["name"]) != alias_key(display):
            operator.setdefault((match_key(s["name"]), match_key(s.get("stone_type") or "")), set()).add(
                alias_key(display))
    learned: dict[str, set[str]] = {}
    for row in decisions_store.active_learned_aliases():
        learned.setdefault(row["identity_key"], set()).add(row["norm"])
    return Stores(seed=_frozen(seed), operator=_frozen(operator), learned=_frozen(learned),
                  is_code=code_matcher(active_pack().name_code_pattern))


def _frozen(d: dict) -> dict:
    return {k: frozenset(v) for k, v in d.items()}


# --- the file on disk and on S3 --------------------------------------------------------------------------------------
def _to_upload_dir() -> Path:
    from stone_pipeline.config.settings import SETTINGS
    return SETTINGS.paths.to_upload_dir


def _export_file() -> Path:
    from stone_pipeline.config.settings import SETTINGS
    return SETTINGS.paths.variants_export_csv


def _review_dir() -> Path:
    from stone_pipeline.config.settings import SETTINGS
    return SETTINGS.paths.review_dir


def _s3_client():
    from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client
    return aws_client("s3", config=BOTOCORE_DEFAULTS)


def _bucket_prefix() -> tuple[str, str]:
    from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET
    return S3_BUCKET, f"{ENV_SEGMENT}/scraper/to_upload"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def digit_items(classification: Classification) -> list[tuple[str, str, str, str]]:
    """The open items of the served aliases (seed, operator, learned) holding a digit: the code pattern did not accept
    them (a code is classed `code`), so staff decide whether each is a code."""
    return [("digit_non_code", key, value, open_items.DIGIT_REASON) for key, value, cls in classification.rows
            if cls in ALIAS_CLASSES and any(ch.isdigit() for ch in value)]


def admin_items(export: Path, is_code: Callable[[str], bool]) -> Optional[list[tuple[str, str, str, str]]]:
    """The open items of the admin aliases (the live export's AdminAliases) failing the boundary, judged with C2's
    context over the export's own rows; never changed by the cleanup (design 3j), only listed for staff. None when
    the export has no AdminAliases column (before P2-7's export split), said in the log."""
    with Path(export).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
        columns = rows[0].keys() if rows else []
    if ADMIN_ALIASES_COLUMN not in columns:
        log.warning("the live export has no AdminAliases column: no admin alias is judged (P2-7 not live yet)",
                    extra={"extra_fields": {"export": str(export)}})
        return None
    contexts = build_contexts((r["Key"], r["Type"], r["Name"]) for r in rows if r.get("Key"))
    items = []
    for r in rows:
        for value in (v.strip() for v in (r[ADMIN_ALIASES_COLUMN] or "").split("|")):
            if value and (why := alias_refusal(value, contexts[r["Key"]], is_code)):
                items.append(("admin_refused", r["Key"], value, why))
    return sorted(items, key=lambda i: (i[0], i[1], alias_key(i[2]), i[2]))


@dataclass(frozen=True)
class Built:
    classification: Classification
    generated_at: str                          # the moment the synced check passed
    open_items: int
    admin_rows: str                            # "read", or "absent" (the export has no AdminAliases column yet)


def build(ledger) -> Built:
    """Classify the ledger against this env's stores, write the difference list and the open review items (always,
    for the owner and staff), then refuse unless the file is whole."""
    stores = load_stores()
    classification = classify(ledger, stores)
    generated_at = _now()
    export = _export_file()
    admin = admin_items(export, stores.is_code) if export.exists() else None
    items = digit_items(classification) + (admin or [])
    review = _review_dir()
    review.mkdir(parents=True, exist_ok=True)
    (review / DIFFERENCES_FILE).write_bytes(differences_bytes(classification))
    (review / open_items.OPEN_FILE).write_bytes(open_items.open_items_bytes(items))
    require_whole(classification)
    return Built(classification, generated_at, len(items), "read" if admin is not None else "absent")


def publish(ledger) -> dict:
    """The route's work (lifecycle.alias_classification): build, then merge the file's entry and the
    `alias_classification` field into the PUBLISHED manifest, carrying every other entry and the produce's run id.
    It never reads the local to_upload (empty on the service after a roll until the next produce). Read-only on the
    ledger and config.db; an absent or unreadable published manifest refuses (deliverables.merge_into_published)."""
    from stone_pipeline.io.deliverables import merge_into_published
    built = build(ledger)
    classification, generated_at = built.classification, built.generated_at
    bucket, prefix = _bucket_prefix()
    entry = merge_into_published(_s3_client(), bucket, prefix, FILE_NAME, csv_bytes(classification),
                                 MANIFEST_FIELD, manifest_field(classification, generated_at))
    return {"file": FILE_NAME, "rows": entry["rows"], "sha256": entry["sha256"],
            "variations": classification.variations, "tombstoned": classification.tombstoned,
            "removed": sum(d[2] == "removed" for d in classification.differences), "generated_at": generated_at,
            "open_items": built.open_items, "admin_rows": built.admin_rows}


def main() -> int:
    """Write the file and the difference list locally from this env's ledger (read-only): 0, or 3 on a refusal (the
    difference list is still written)."""
    from stone_pipeline.ledger import writethrough
    with writethrough.connect(bind=False) as ledger:
        try:
            built = build(ledger)
        except (NotAllSynced, Unclassifiable) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 3
    classification = built.classification
    (_to_upload_dir() / FILE_NAME).write_bytes(csv_bytes(classification))
    print(f"{FILE_NAME}: {len(classification.rows)} rows, {classification.variations} variations, "
          f"{classification.tombstoned} tombstoned, generated_at {built.generated_at}; differences in "
          f"{DIFFERENCES_FILE}; {built.open_items} open item(s) in {open_items.OPEN_FILE} "
          f"(admin rows {built.admin_rows})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
