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

The new Key's name is the identity's CANONICAL Name (the stored Name with any bracketed alias split off,
D1 rev 8), found by joining the old-Key MIDDLE (branch and uuid stripped) across stores: the bracket
identities carry the bracketed slug in every old Key but one clean Name, so a row-less Key takes that Name
and never parses its own slug (which would mint a second new Key).

Aborts (each lists the offending Keys, then the run fails):
  - name_tie: one identity carries two canonical names (D1 tie rule).
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

from stone_pipeline.config.categories import CATEGORIES
from stone_pipeline.config.settings import ATTRIBUTES_STATUS_REQUIRED
from stone_pipeline.core.text import split_bracket_alias
from stone_pipeline.phase1.variety_key import _slug_us, new_key
from stone_pipeline.reference.medusa_files import read_attributes

# --- config ---------------------------------------------------------------------------------------------
ENVS = ("development", "production")           # every env whose stores are unioned
BRANCH_ORDER = tuple(c.name for c in CATEGORIES)  # the pack's category order -> category rank (not hardcoded)
OUTPUT_CSV = "variety_key_map.csv"              # written in the output dir; its sha256 is printed
CSV_COLUMNS = ("old_key", "new_key", "category_rank", "type_name")

# The real run reads a PRIVATE frozen-snapshot copy, never a live store. The snapshot root holds one folder
# per env (<root>/<env>/...), each with the stores named below; this is the repeatable "every store" proof
# (one config block, not a scattered list of paths). A store missing from the snapshot aborts the run, naming the env
# and the store (MissingStore). The S3 base copies sit beside the committed base (the spec's source row names both).
SNAPSHOT_STORES = {
    "ledger_db": ("ledger.db",),
    "config_db": ("config.db",),
    "base_csv": ("variants_export_base.csv",),
    "base_copies": ("s3_base/variants_export_base.csv",),
    "attributes_csv": ("attributes.csv",),
    "backbone_files": ("backbone_slabs.json", "backbone_blocks.json", "backbone_tiles.json"),
    "backbone_additions": ("backbone_additions/slab.json", "backbone_additions/block.json",
                           "backbone_additions/tile.json"),
}
# Every configured store is REQUIRED: build_stores ABORTS and names any absent from the snapshot, so a
# silently skipped store can never shrink the "every store" proof (spec rev 8 section 3 names each one). A
# multi-file store is required to have at least one file. The S3 base copies are the deployed task's authority
# on a stack and have diverged from the committed base before (an S3 base edit git never saw), so they are a
# store in their own right, not a cross-check. No store is optional.
REQUIRED_STORES = ("ledger_db", "config_db", "base_csv", "base_copies", "attributes_csv",
                   "backbone_files", "backbone_additions")


class MissingStore(Exception):
    """A required store is absent from the snapshot. Raised by build_stores so the run fails loud and named."""


@dataclass(frozen=True)
class StorePaths:
    """Where one env's stores live (a private frozen-snapshot copy at run time, a fixture in tests)."""
    env: str
    ledger_db: Path | None = None
    config_db: Path | None = None
    base_csv: Path | None = None
    base_copies: tuple[Path, ...] = ()           # the S3 base copies the spec's source row names
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
    untyped: list[str] = field(default_factory=list)              # old Keys held (stone_type ''): no new Key
    ghosts: list[str] = field(default_factory=list)               # backbone-only Keys: mapped, no store row
    merged: list[str] = field(default_factory=list)               # new Keys fed by two middles (N1 review)
    aborts: dict[str, list[str]] = field(default_factory=dict)    # kind -> listed detail lines

    @property
    def ok(self) -> bool:
        return not self.aborts


# which stores' Name is authoritative. config.db Keys carry no Name; a backbone `variant` spelling is a last
# resort only, and never feeds the tie rule (C1: 162 identities tied on backbone case/accent drift alone).
_STORE_NAME_STORES = ("ledger", "base_csv")
_BACKBONE_STORES = ("backbone", "backbone_additions")


# --- readers (thin; each yields Records) ----------------------------------------------------------------
def _rows(db: Path, sql: str):
    # immutable=1 (not mode=ro): a frozen private copy is never written, and this also stops SQLite creating
    # the -wal / -shm sidecars beside it, so the read leaves the snapshot byte-for-byte unchanged.
    con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
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
    parsed or row type resolves only when its slug maps to EXACTLY one name here. Archived and internal types
    are kept: an old Key may carry a type Medusa has since archived, and it still has to resolve."""
    vocab: dict[str, set[str]] = {}
    for path in attributes_paths:
        for a in read_attributes(Path(path), require_status=ATTRIBUTES_STATUS_REQUIRED):
            if a.category.lower() == "type":
                vocab.setdefault(_slug_us(a.value), set()).add(a.value)
    return vocab


# --- type parse (NEW offline code; not the production parser) --------------------------------------------
def _branch_of(old_key: str) -> str:
    return old_key.split("_", 1)[0]


def _middle(old_key: str) -> str:
    """The identity core of an OLD Key `{branch}_{slug_type}_{slug_name}_{uuid}`: branch and uuid removed.
    Every copy of one identity (its slab/block/tile Keys, and any retired/protected copy with no store row)
    carries the SAME middle, so it is the join key by which a string-only Key finds its identity and that
    identity's canonical Name. The uuid is the final `_`-token; the branch is the first."""
    without_uuid = old_key.rsplit("_", 1)[0]
    return without_uuid.split("_", 1)[1] if "_" in without_uuid else ""


def _canonical(name: str | None) -> str | None:
    """The identity's canonical Name: the stored Name with any bracketed alias pulled off (D1, rev 8). The old
    Keys of the bracket identities carry the bracketed slug, but the new Key's name must be the clean Name, so
    the name slug is NEVER read from the old Key."""
    return split_bracket_alias(name)[0] if name else None


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
def _type_for_slug(slug: str | None, types: set[str]) -> str | None:
    """The type in `types` whose slug is `slug`: a conflicting old Key's OWN type (the one its slug encodes)."""
    return next((t for t in types if _slug_us(t) == slug), None)


def build_key_map(records: list[Record], type_vocab: dict[str, set[str]]) -> KeyMapResult:
    """Pure union engine. Deterministic: every grouping and output is sorted, nothing leans on dict/set order,
    so two runs under different PYTHONHASHSEED give byte-identical output.

    An identity is keyed by the old-Key MIDDLE (branch and uuid stripped): every copy and any row-less copy
    shares it. The canonical Name comes from STORE rows (ledger, base copies) only; a backbone `variant`
    spelling is a last-resort Name and never feeds the tie rule (C1). The type comes from the ledger or a
    backbone post."""
    res = KeyMapResult()
    by_key: dict[str, list[Record]] = {}
    for rec in records:
        by_key.setdefault(rec.old_key, []).append(rec)

    store_names_of: dict[str, set[str]] = {}       # middle -> canonical Names from STORE rows (tie + new Key)
    backbone_names_of: dict[str, set[str]] = {}    # middle -> canonical Names from backbone posts (fallback)
    types_of: dict[str, set[str]] = {}
    untyped_mids: set[str] = set()
    store_backed: set[str] = set()                 # old Keys a store carries (not backbone-only)
    known_type_slugs = set(type_vocab)
    for rec in records:
        if _branch_of(rec.old_key) not in BRANCH_ORDER:   # L1: a malformed Key never pools with real identities
            continue
        mid = _middle(rec.old_key)
        canon = _canonical(rec.name)
        if canon:
            target = store_names_of if rec.store in _STORE_NAME_STORES else backbone_names_of
            target.setdefault(mid, set()).add(canon)
        if rec.type_name:
            types_of.setdefault(mid, set()).add(rec.type_name)
            known_type_slugs.add(_slug_us(rec.type_name))
        if rec.untyped:
            untyped_mids.add(mid)
        if rec.store not in _BACKBONE_STORES:
            store_backed.add(rec.old_key)

    protected_to_new: dict[str, set[str]] = {}
    new_to_entries: dict[str, list[tuple[int, str, bool]]] = {}   # new Key -> (rank, old Key, store-backed?)
    new_to_mids: dict[str, set[str]] = {}

    for old_key in sorted(by_key):
        recs = by_key[old_key]
        branch = _branch_of(old_key)
        if branch not in BRANCH_ORDER:                 # L1
            res.aborts.setdefault("unresolved_branch", []).append(old_key)
            continue
        mid = _middle(old_key)

        store_names = store_names_of.get(mid, set())
        if len(store_names) > 1:                        # D1 tie: STORE Names only (C1)
            res.aborts.setdefault("name_tie", []).append(old_key)
            continue
        canonical_name = (next(iter(store_names)) if store_names
                          else (sorted(backbone_names_of[mid])[0] if backbone_names_of.get(mid) else None))

        types = types_of.get(mid, set())
        parsed = parse_type_slug(old_key, known_type_slugs)
        if len(types) > 1:                              # M1: keep the abort, name both sides
            key_type = _type_for_slug(parsed[0], types) if parsed else None
            posts = ",".join(sorted(t for t in types if t != key_type))
            res.aborts.setdefault("type_conflict", []).append(
                f"{old_key} (key={key_type or '?'}, post={posts})")
            continue
        slug_type = _slug_us(next(iter(types))) if types else (parsed[0] if parsed else None)

        if slug_type is None:                           # names no type: held if the store said '', else abort
            target = res.untyped if (mid in untyped_mids) else res.aborts.setdefault("unresolved_type", [])
            target.append(old_key)
            continue

        vocab_names = type_vocab.get(slug_type)
        if not vocab_names or len(vocab_names) != 1:
            res.aborts.setdefault("unresolved_type", []).append(old_key)
            continue
        type_name = next(iter(vocab_names))             # the one live Medusa type name

        name_for_key = canonical_name if canonical_name is not None else (parsed[1] if parsed else None)
        if name_for_key is None:                        # a row-less Key that joins no named row and cannot parse
            res.aborts.setdefault("unresolved_type", []).append(old_key)
            continue

        nk = new_key(type_name, name_for_key)           # _slug_us is idempotent on an already-slugged fallback
        rank = BRANCH_ORDER.index(branch)
        res.rows.append(MapRow(old_key, nk, rank, type_name))
        new_to_entries.setdefault(nk, []).append((rank, old_key, old_key in store_backed))
        new_to_mids.setdefault(nk, set()).add(mid)
        if old_key not in store_backed:
            res.ghosts.append(old_key)                  # M2: backbone-only Key, mapped and listed
        if any(r.protected for r in recs):
            protected_to_new.setdefault(nk, set()).add(old_key)

    # M2: abort only when two STORE-backed old Keys of ONE category land on one new Key (that breaks D2's
    # per-env, per-category survivor pick); backbone-only ghosts colliding are listed, not aborted.
    for nk, entries in sorted(new_to_entries.items()):
        by_rank: dict[int, list[str]] = {}
        for rank, old_key, backed in entries:
            if backed:
                by_rank.setdefault(rank, []).append(old_key)
        for rank in sorted(by_rank):
            if len(by_rank[rank]) > 1:
                res.aborts.setdefault("category_collision", []).extend(sorted(by_rank[rank]))

    # M3: a new Key fed by more than one middle merges two Medusa records; a non-abort list for N1 review.
    res.merged = sorted(nk for nk, mids in new_to_mids.items() if len(mids) > 1)

    for nk, keys in sorted(protected_to_new.items()):
        if len(keys) > 1:
            res.aborts.setdefault("protected_collision", []).extend(sorted(keys))

    res.rows.sort(key=lambda m: (m.new_key, m.category_rank, m.old_key))
    for lst in (res.untyped, res.ghosts):
        lst.sort()
    for kind in res.aborts:
        res.aborts[kind] = sorted(set(res.aborts[kind]))
    return res


# --- output ---------------------------------------------------------------------------------------------
def write_map(result: KeyMapResult, out_csv: Path) -> str:
    """Write the map CSV deterministically and return its sha256 hex digest."""
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 with no BOM and `\n` line ends (not the csv default `\r\n`), to match the repo's other writers and
    # fd's P1-8 parser; QUOTE_MINIMAL, so a type name with a comma is quoted.
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(CSV_COLUMNS)
        for m in result.rows:
            w.writerow((m.old_key, m.new_key, m.category_rank, m.type_name))
    return hashlib.sha256(out_csv.read_bytes()).hexdigest()


def build_stores(snapshot_root: Path, envs: tuple[str, ...] = ENVS) -> list[StorePaths]:
    """Assemble each env's StorePaths from a private frozen-snapshot root (SNAPSHOT_STORES layout). EVERY listed
    file of every store must be present, or this ABORTS (MissingStore) naming each absent one: the proof is
    "every store", so a missing file is never skipped silently (a multi-file store with one file gone would drop
    that file's Keys). No store is optional."""
    root = Path(snapshot_root)

    def present(rels: tuple[str, ...], env: str) -> tuple[Path, ...]:
        return tuple(root / env / r for r in rels if (root / env / r).exists())

    stores, missing = [], []
    for env in envs:
        got = {k: present(v, env) for k, v in SNAPSHOT_STORES.items()}
        for k in REQUIRED_STORES:                    # present == listed: name every absent file, not just a store
            missing += [f"{env}/{r}" for r in SNAPSHOT_STORES[k] if not (root / env / r).exists()]
        stores.append(StorePaths(
            env=env,
            ledger_db=got["ledger_db"][0] if got["ledger_db"] else None,
            config_db=got["config_db"][0] if got["config_db"] else None,
            base_csv=got["base_csv"][0] if got["base_csv"] else None,
            attributes_csv=got["attributes_csv"][0] if got["attributes_csv"] else None,
            base_copies=got["base_copies"],
            backbone_files=got["backbone_files"],
            backbone_additions=got["backbone_additions"]))
    if missing:
        raise MissingStore("required store file(s) absent from the snapshot: " + ", ".join(sorted(missing)))
    return stores


def collect_records(stores: list[StorePaths]) -> list[Record]:
    records: list[Record] = []
    for s in stores:
        if s.ledger_db:
            records += list(ledger_records(s.ledger_db, s.env))
        if s.config_db:
            records += list(config_db_records(s.config_db, s.env))
        for base in ((s.base_csv,) if s.base_csv else ()) + s.base_copies:
            records += list(base_csv_records(base, s.env))
        if s.backbone_files:
            records += list(backbone_records(s.backbone_files, s.env))
        if s.backbone_additions:
            records += list(backbone_records(s.backbone_additions, s.env, store="backbone_additions"))
    return records


def stores_read(stores: list[StorePaths]) -> list[str]:
    """The stores actually present, for the 'every store' proof line."""
    read = []
    for s in stores:
        here = [name for name, val in (("ledger", s.ledger_db), ("config.db", s.config_db),
                                       ("base", s.base_csv), ("attributes", s.attributes_csv)) if val]
        here += [f"{len(s.base_copies)} base_copies"] if s.base_copies else []
        here += [f"{len(s.backbone_files)} backbone"] if s.backbone_files else []
        here += [f"{len(s.backbone_additions)} additions"] if s.backbone_additions else []
        read.append(f"{s.env}: {', '.join(here) or '(none)'}")
    return read


def generate(stores: list[StorePaths], out_csv: Path) -> tuple[KeyMapResult, str | None]:
    """Build the map from the given stores and write it. The digest is returned only when the build is clean;
    an aborting build writes no map and returns the aborts in the result for the caller to list."""
    vocab = load_type_vocab([s.attributes_csv for s in stores if s.attributes_csv])
    result = build_key_map(collect_records(stores), vocab)
    digest = write_map(result, out_csv) if result.ok else None
    return result, digest


def main(stores: list[StorePaths], out_dir: Path) -> int:
    for line in stores_read(stores):
        print("store", line)
    result, digest = generate(stores, out_dir / OUTPUT_CSV)
    print(f"mapped {len(result.rows)} old Keys; {len(result.untyped)} untyped held, "
          f"{len(result.ghosts)} backbone-only ghosts, {len(result.merged)} merged (N1 review)")
    for label, items in (("untyped", result.untyped), ("ghost", result.ghosts), ("merged", result.merged)):
        if items:
            print(f"  {label} ({len(items)}): {', '.join(items)}")
    if not result.ok:
        for kind, keys in sorted(result.aborts.items()):
            print(f"ABORT {kind} ({len(keys)}): {', '.join(keys)}")
        return 1
    print(f"wrote {out_dir / OUTPUT_CSV}\nsha256 {digest}")
    return 0
