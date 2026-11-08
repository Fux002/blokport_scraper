"""Phase 1 SC-1: the ONE union variety-key map generator (offline tool, C9 / spec section 5 step 2).

Reads every store that carries an OLD variation Key, in each env, and writes one union map
`(old_key, new_key, category_rank, type_name)`: a Key present in several stores or envs maps to ONE new Key
(D1), so an identity's slab/block/tile copies and any retired-only copy collapse together. The new Key is
`phase1.variety_key.new_key`. The map build is the proof "every Key in every store is mapped": it ABORTS and
lists, rather than guessing, when a Key cannot resolve.

Scope (SC-1): offline, deterministic, additive. It reads real stores only on PRIVATE copies, read-only, and
writes a local CSV plus its sha256 digest, never S3 and never a live store. It changes no matching, type,
curate, ledger or sync code; the type parse here is NEW offline code, not the production parser. The Medusa
survivor map (D2) and `scraper_sync_ref` are the backend's, later, and are not read here.

Stores unioned (the scraper manager, 8 Nov): the ledger `variation` table (all rows, `in_full` 0 included),
config.db `retired_variation` and `protected_variation` (Keys), the committed base copies
(`variants_export_base.csv`), the backbone files and `backbone_additions`.

Aborts (each lists the offending Keys, then the run fails):
  - name_tie: one old Key carries different display names (D1 tie rule).
  - unresolved_type: a row-less Key whose type cannot be parsed, or whose parsed/row type does not map to
    exactly one Medusa type name in the vocabulary (`from_medusa/<env>/attributes.csv` type rows, both envs).
  - type_conflict: one old Key carries two different types across stores.
  - unresolved_branch: a Key whose leading branch is not a pack category (no category rank).
  - protected_collision: two different PROTECTED Keys map to one new Key.

Untyped identities (`stone_type` '') are NOT an abort: they get no new Key and are listed (held for review).
No argparse, no em dashes. Config is the block below.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.stages.curate.keys import BRANCHES, _slug_us

# --- config ---------------------------------------------------------------------------------------------
ENVS = ("development", "production")           # every env whose stores are unioned
BRANCH_ORDER = BRANCHES                         # the pack's category order -> category rank (not hardcoded)
OUTPUT_CSV = "variety_key_map.csv"              # written in the output dir; its sha256 is printed
CSV_COLUMNS = ("old_key", "new_key", "category_rank", "type_name")


@dataclass(frozen=True)
class StorePaths:
    """Where one env's stores live (a private frozen-snapshot copy at run time, a fixture in tests)."""
    env: str
    ledger_db: Path | None = None
    config_db: Path | None = None
    base_csv: Path | None = None
    backbone_files: tuple[Path, ...] = ()
    backbone_additions: tuple[Path, ...] = ()
    attributes_csv: Path | None = None


@dataclass(frozen=True)
class Record:
    """One (old Key) sighting in one store. `type_name`/`name` are present only where the store carries them
    (the ledger and the backbones); the base CSV carries a name only, config.db Keys carry neither."""
    env: str
    store: str
    old_key: str
    name: str | None = None
    type_name: str | None = None
    protected: bool = False
    untyped: bool = False          # the store marks it type-less (stone_type ''): held, never guessed


@dataclass
class MapRow:
    old_key: str
    new_key: str
    category_rank: int
    type_name: str


@dataclass
class KeyMapResult:
    rows: list[MapRow] = field(default_factory=list)
    untyped: list[str] = field(default_factory=list)              # old Keys held (no new Key)
    aborts: dict[str, list[str]] = field(default_factory=dict)    # kind -> old Keys

    @property
    def ok(self) -> bool:
        return not self.aborts


# --- readers (thin; each yields Records) ----------------------------------------------------------------
def _rows(db: Path, sql: str):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        yield from con.execute(sql)
    finally:
        con.close()


def ledger_records(db: Path, env: str):
    for r in _rows(db, "SELECT key, type, name FROM variation"):
        type_name = (r["type"] or "").strip()
        yield Record(env, "ledger", r["key"], name=(r["name"] or "").strip() or None,
                     type_name=type_name or None, untyped=(type_name == ""))


def config_db_records(db: Path, env: str):
    for r in _rows(db, "SELECT key FROM retired_variation"):
        yield Record(env, "config.retired", r["key"])
    for r in _rows(db, "SELECT key FROM protected_variation"):
        yield Record(env, "config.protected", r["key"], protected=True)


def base_csv_records(path: Path, env: str):
    with path.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            key = (r.get("Key") or "").strip()
            if key:
                yield Record(env, "base_csv", key, name=(r.get("Name") or "").strip() or None)


def _posts(doc) -> list:
    return doc.get("posts", []) if isinstance(doc, dict) else (doc if isinstance(doc, list) else [])


def backbone_records(paths, env: str, store: str = "backbone"):
    for path in paths:
        for post in _posts(json.loads(Path(path).read_text(encoding="utf-8"))):
            key = (post.get("key") or "").strip()
            if not key:                       # keyless posts join by (type, name) later; no old Key to map
                continue
            type_name = (post.get("stone_type") or "").strip()
            yield Record(env, store, key, name=(post.get("variant") or "").strip() or None,
                         type_name=type_name or None, untyped=(type_name == ""))


def load_type_vocab(attributes_paths) -> dict[str, set[str]]:
    """slug -> the set of Medusa type names with that slug, across every env's attributes.csv type rows. A
    parsed or row type resolves only when its slug maps to EXACTLY one name here."""
    vocab: dict[str, set[str]] = {}
    for path in attributes_paths:
        with Path(path).open(encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                if (r.get("category") or "").strip().lower() == "type":
                    name = (r.get("value") or "").strip()
                    if name:
                        vocab.setdefault(_slug_us(name), set()).add(name)
    return vocab


# --- type parse (NEW offline code; not the production parser) --------------------------------------------
def _branch_of(old_key: str) -> str:
    return old_key.split("_", 1)[0]


def parse_type_slug(old_key: str, known_type_slugs) -> tuple[str, str] | None:
    """(slug_type, slug_name) for a row-less Key `{branch}_{slug_type}_{slug_name}_{uuid}`, by longest match of
    the type slug. The uuid is the final `_`-token (hyphenated, no underscore); the branch is the first. None
    when no known type slug is a prefix, or the name part is empty."""
    without_uuid = old_key.rsplit("_", 1)[0]
    branch = _branch_of(old_key)
    if "_" not in without_uuid or branch not in BRANCH_ORDER:
        return None
    middle = without_uuid[len(branch) + 1:]           # {slug_type}_{slug_name}
    best = None
    for slug in known_type_slugs:
        if (middle == slug or middle.startswith(slug + "_")) and (best is None or len(slug) > len(best)):
            best = slug
    if best is None:
        return None
    slug_name = middle[len(best):].lstrip("_")
    return (best, slug_name) if slug_name else None


# --- engine ---------------------------------------------------------------------------------------------
def build_key_map(records: list[Record], type_vocab: dict[str, set[str]]) -> KeyMapResult:
    """Pure union engine. Deterministic: every grouping and output is sorted, nothing leans on dict/set order,
    so two runs under different PYTHONHASHSEED give byte-identical output."""
    res = KeyMapResult()
    by_key: dict[str, list[Record]] = {}
    for rec in records:
        by_key.setdefault(rec.old_key, []).append(rec)

    known_type_slugs = set(type_vocab)
    for recs in by_key.values():
        for r in recs:
            if r.type_name:
                known_type_slugs.add(_slug_us(r.type_name))

    protected_to_new: dict[str, set[str]] = {}

    for old_key in sorted(by_key):
        recs = by_key[old_key]
        typed = [r for r in recs if r.type_name]
        names = sorted({r.name for r in recs if r.name})
        protected = any(r.protected for r in recs)

        if typed:
            types = sorted({r.type_name for r in typed})
            if len(types) > 1:
                res.aborts.setdefault("type_conflict", []).append(old_key)
                continue
            type_name, name = types[0], (names[0] if names else _slug_us(types[0]))
            slug_type, slug_name = _slug_us(type_name), _slug_us(name)
        elif any(r.untyped for r in recs):
            res.untyped.append(old_key)            # store marks it type-less (stone_type ''): held, never guessed
            continue
        else:
            parsed = parse_type_slug(old_key, known_type_slugs)
            if parsed is None:
                res.aborts.setdefault("unresolved_type", []).append(old_key)
                continue
            slug_type, slug_name = parsed
            type_name = None   # resolved from the vocabulary below

        if len(names) > 1:                          # D1 tie: one old Key, different display names
            res.aborts.setdefault("name_tie", []).append(old_key)
            continue

        vocab_names = type_vocab.get(slug_type)
        if not vocab_names or len(vocab_names) != 1:
            res.aborts.setdefault("unresolved_type", []).append(old_key)
            continue
        type_name = next(iter(vocab_names))         # the one live Medusa type name

        branch = _branch_of(old_key)
        if branch not in BRANCH_ORDER:
            res.aborts.setdefault("unresolved_branch", []).append(old_key)
            continue

        nk = new_key(type_name, slug_name)          # _slug_us is idempotent on the already-slugged name
        res.rows.append(MapRow(old_key, nk, BRANCH_ORDER.index(branch), type_name))
        if protected:
            protected_to_new.setdefault(nk, set()).add(old_key)

    for nk, keys in sorted(protected_to_new.items()):
        if len(keys) > 1:
            res.aborts.setdefault("protected_collision", []).extend(sorted(keys))

    res.rows.sort(key=lambda m: (m.new_key, m.category_rank, m.old_key))
    res.untyped.sort()
    for kind in res.aborts:
        res.aborts[kind] = sorted(set(res.aborts[kind]))
    # a Key caught by protected_collision stays a valid row too; the abort just stops the run for review.
    return res


# --- output ---------------------------------------------------------------------------------------------
def write_map(result: KeyMapResult, out_csv: Path) -> str:
    """Write the map CSV deterministically and return its sha256 hex digest."""
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for m in result.rows:
            w.writerow((m.old_key, m.new_key, m.category_rank, m.type_name))
    return hashlib.sha256(out_csv.read_bytes()).hexdigest()


def collect_records(stores: list[StorePaths]) -> list[Record]:
    records: list[Record] = []
    for s in stores:
        if s.ledger_db:
            records += list(ledger_records(s.ledger_db, s.env))
        if s.config_db:
            records += list(config_db_records(s.config_db, s.env))
        if s.base_csv:
            records += list(base_csv_records(s.base_csv, s.env))
        if s.backbone_files:
            records += list(backbone_records(s.backbone_files, s.env))
        if s.backbone_additions:
            records += list(backbone_records(s.backbone_additions, s.env, store="backbone_additions"))
    return records


def generate(stores: list[StorePaths], out_csv: Path) -> tuple[KeyMapResult, str | None]:
    """Build the map from the given stores and write it. The digest is returned only when the build is clean;
    an aborting build writes no map and returns the aborts in the result for the caller to list."""
    vocab = load_type_vocab([s.attributes_csv for s in stores if s.attributes_csv])
    result = build_key_map(collect_records(stores), vocab)
    digest = write_map(result, out_csv) if result.ok else None
    return result, digest


def main(stores: list[StorePaths], out_dir: Path) -> int:
    result, digest = generate(stores, out_dir / OUTPUT_CSV)
    print(f"mapped {len(result.rows)} old Keys; {len(result.untyped)} untyped (held, no new Key)")
    if result.untyped:
        print("  untyped:", ", ".join(result.untyped))
    if not result.ok:
        for kind, keys in sorted(result.aborts.items()):
            print(f"ABORT {kind} ({len(keys)}): {', '.join(keys)}")
        return 1
    print(f"wrote {out_dir / OUTPUT_CSV}\nsha256 {digest}")
    return 0
