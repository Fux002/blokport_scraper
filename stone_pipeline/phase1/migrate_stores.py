"""Phase 1 SC-3: the scraper's store migration to the new variety Keys (PHASE-1-SPEC section 5 step 5).

This module is the PURE core: from the committed key map (SC-1: old Key -> new Key, category rank, type name) and the
env's survivor map (old Key -> the Medusa variation id the backend kept, D2), it decides what happens to every
Key-bearing row. Nothing here reads or writes a store, S3 or the network. The writer (one DDL source: the repo's ledger
schema) applies these decisions to the step-1 snapshot copies and writes the `.v2` stores.

Rules, each from the spec:
- A variety's copies collapse to ONE row under its new Key: the copy whose own `medusa_id` is the survivor id, set
  dirty so it re-serves once (C4, D2). That is the survivor's record, not necessarily the slab copy C4 names (D2 makes
  the survivor per env). Its aliases are every copy's, unioned case-insensitively with the slab copy's casing first;
  its in_full is the max over the copies. An identity the backend has no record of (an unpulled mint) keeps its first
  copy by category rank as one pending row (G8).
- A copy in `retiring` or `gap_held`, or with `abandoned_at`, aborts (section 3); so does a Key outside the key map.
- Products point at their variety's new Key.
- Variation tombstones on old Keys are DROPPED and listed (old Key, new Key, reason), never re-pointed: after the
  collapse the new Key is a live variety, and a re-pointed tombstone would delete it. Product tombstones
  are untouched.
- config.db: a retirement of every copy becomes one variety retirement; a partial one aborts (D8 is not built:
  0 measured). Protected Keys move to the new Key; two on one new Key abort.
- Learned aliases move to the survivor's id (or the new Key for a variety with no Medusa record), deduplicated.

Every result is sorted, so the same inputs give the same decisions whatever the row order. No argparse, no em dashes.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field

# --- config ---------------------------------------------------------------------------------------------------------
KEY_MAP_COLUMNS = ("old_key", "new_key", "category_rank", "type_name")
RETIRED_KEYS_COLUMNS = ("old_key", "step")    # the env's committed retired-Keys file
UNSAFE_STATES = ("retiring", "gap_held")        # section 3, C4: a copy in these states is never collapsed
SURVIVOR_STATE = "dirty"                        # C4: the surviving row re-serves once
PENDING_STATE = "pending"                       # G8: a variety with no Medusa record is pulled after the roll


class MigrationRefused(Exception):
    """An input the migration will not read (wrong digest, a map for another key map): nothing is decided."""


def _digest_checked(data: bytes, sha256: str, what: str) -> None:
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise MigrationRefused(f"{what}: sha256 {got} is not the expected {sha256}")


@dataclass(frozen=True)
class KeyMap:
    """The SC-1 key map: old Key -> (new Key, category rank, type name)."""
    rows: dict

    def new_key(self, old_key: str) -> str | None:
        row = self.rows.get(old_key)
        return row[0] if row else None

    def rank(self, old_key: str) -> int:
        return self.rows[old_key][1]

    def type_name(self, old_key: str) -> str:
        return self.rows[old_key][2]


def read_key_map(data: bytes, sha256: str) -> KeyMap:
    """The key map from its bytes, read only at its committed digest."""
    _digest_checked(data, sha256, "key map")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    if tuple(reader.fieldnames or ()) != KEY_MAP_COLUMNS:
        raise MigrationRefused(f"key map: columns {reader.fieldnames} are not {list(KEY_MAP_COLUMNS)}")
    return KeyMap({r["old_key"]: (r["new_key"], int(r["category_rank"]), r["type_name"]) for r in reader})


def read_retired_keys(data: bytes, sha256: str, key_map: KeyMap) -> dict[str, str]:
    """old Key -> the step that retired it, from the env's committed retired-Keys file, read only at its pinned digest:
    the Keys retired before phase 1 that no store records (a ledger variation tombstone is the other record). Every
    line names a Key outside the key map and cites its step; anything else refuses, so a mapping fault is never
    recorded as a retirement."""
    _digest_checked(data, sha256, "retired keys")
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    if tuple(reader.fieldnames or ()) != RETIRED_KEYS_COLUMNS:
        raise MigrationRefused(f"retired keys: columns {reader.fieldnames} are not {list(RETIRED_KEYS_COLUMNS)}")
    out: dict[str, str] = {}
    for row in reader:
        key, step = (row["old_key"] or "").strip(), (row["step"] or "").strip()
        if not key:
            raise MigrationRefused(f"retired keys: a line with no Key ({row})")
        if key in out:
            raise MigrationRefused(f"retired keys: {key} is listed twice")
        if not step:
            raise MigrationRefused(f"retired keys: {key} cites no step that retired it")
        if key_map.new_key(key) is not None:
            raise MigrationRefused(f"retired keys: {key} is in the key map, so it is a live Key, not a retired one")
        out[key] = step
    return out


def read_survivor_map(data: bytes, sha256: str, key_map_sha256: str) -> dict[str, str]:
    """old Key -> survivor id from P1-23's expected-survivor-map.json, read only at its digest and only when it was
    made from THIS key map (its keyMapSha256)."""
    _digest_checked(data, sha256, "survivor map")
    doc = json.loads(data.decode("utf-8"))
    if doc.get("keyMapSha256") != key_map_sha256:
        raise MigrationRefused(f"survivor map: keyMapSha256 {doc.get('keyMapSha256')} is not this key map's "
                               f"{key_map_sha256}")
    return {s["oldKey"]: s["survivorId"] for s in doc["survivors"]}


def _abort(aborts: dict, kind: str, item: str) -> None:
    aborts.setdefault(kind, []).append(item)


def _sorted_aborts(aborts: dict) -> dict:
    return {kind: sorted(items) for kind, items in sorted(aborts.items())}


@dataclass
class VariationPlan:
    """rows: new Key -> the surviving row (its `key` the new Key); collapsed: new Key -> its old copies, by rank;
    aborts: kind -> the Keys (or new Keys) that stop the migration."""
    rows: dict = field(default_factory=dict)
    collapsed: dict = field(default_factory=dict)
    aborts: dict = field(default_factory=dict)


def plan_variations(rows: list[dict], key_map: KeyMap, survivors: dict[str, str]) -> VariationPlan:
    """The ledger's variation rows collapsed to one per new Key (module rules)."""
    plan = VariationPlan()
    groups: dict[str, list[dict]] = {}
    for row in sorted(rows, key=lambda r: r["key"]):
        new = key_map.new_key(row["key"])
        if new is None:
            _abort(plan.aborts, "unmapped_key", row["key"])
            continue
        if row["state"] in UNSAFE_STATES or row.get("abandoned_at"):
            _abort(plan.aborts, "unsafe_state", row["key"])
            continue
        groups.setdefault(new, []).append(row)
    for new, copies in sorted(groups.items()):
        copies.sort(key=lambda r: (key_map.rank(r["key"]), r["key"]))
        ids = {survivors[r["key"]] for r in copies if r["key"] in survivors}
        if len(ids) > 1:
            _abort(plan.aborts, "two_survivors", new)
            continue
        if ids:
            (survivor_id,) = ids
            kept = next((r for r in copies if r.get("medusa_id") == survivor_id), None)
            if kept is None:
                _abort(plan.aborts, "survivor_not_in_ledger", new)
                continue
            state = SURVIVOR_STATE
        else:
            if any(r.get("medusa_id") for r in copies):
                _abort(plan.aborts, "no_survivor_but_synced", new)
                continue
            kept, state = copies[0], PENDING_STATE
        plan.rows[new] = {**kept, "key": new, "state": state, "aliases": _alias_union(copies, kept),
                          "in_full": max(int(r.get("in_full") or 0) for r in copies)}
        plan.collapsed[new] = [r["key"] for r in copies]
    plan.aborts = _sorted_aborts(plan.aborts)
    return plan


def _alias_union(copies: list[dict], kept: dict):
    """The copies' aliases (JSON arrays), case-insensitively unioned in category-rank order, so the slab copy's casing
    wins (spec section 3, Aliases); the kept row's own value when no copy has any."""
    seen, out = set(), []
    for row in copies:
        for alias in json.loads(row["aliases"]) if row.get("aliases") else []:
            if alias.casefold() not in seen:
                seen.add(alias.casefold())
                out.append(alias)
    return json.dumps(out) if out else kept.get("aliases")


def remap_products(products: list[dict], key_map: KeyMap) -> tuple[list[dict], dict]:
    """Every product's variation_key moved to its variety's new Key; a Key outside the map aborts (by SKU)."""
    out, aborts = [], {}
    for p in sorted(products, key=lambda r: r["sku"]):
        old = p.get("variation_key")
        if old is None:
            out.append(dict(p))
            continue
        new = key_map.new_key(old)
        if new is None:
            _abort(aborts, "unmapped_key", p["sku"])
            continue
        out.append({**p, "variation_key": new})
    return out, _sorted_aborts(aborts)


def drop_variation_tombstones(removed: list[dict], key_map: KeyMap) -> tuple[list[dict], list[dict]]:
    """(the tombstones kept: every product one; the variation tombstones dropped, listed with their new Key and
    reason). A variation tombstone on a Key outside the map is listed with new_key None."""
    kept, listed = [], []
    for r in sorted(removed, key=lambda t: t["external_id"]):
        if r["kind"] != "variation":
            kept.append(r)
            continue
        listed.append({"old_key": r["external_id"], "new_key": key_map.new_key(r["external_id"]),
                       "reason": r.get("reason")})
    return kept, listed


def remap_retired(retired: set[str], key_map: KeyMap, copies: set[str]) -> tuple[set[str], dict]:
    """(the new Keys retired, aborts): retired old Keys become their variety's new Key when EVERY copy of that variety
    known in this env (`copies`: the Keys of its ledger, retired and protected rows) is retired; a partial one aborts."""
    aborts: dict = {}
    by_new: dict[str, set[str]] = {}
    for old in copies | retired:
        new = key_map.new_key(old)
        if new is None:
            if old in retired:
                _abort(aborts, "unmapped_key", old)
            continue
        by_new.setdefault(new, set()).add(old)
    out = set()
    for new, olds in sorted(by_new.items()):
        hit = olds & retired
        if not hit:
            continue
        if hit == olds:
            out.add(new)
        else:
            _abort(aborts, "partial_retirement", new)
    return (set() if aborts else out), _sorted_aborts(aborts)


def remap_protected(protected: set[str], key_map: KeyMap) -> tuple[set[str], dict]:
    """(the new Keys protected, aborts): two protected Keys on one new Key abort (as the key map's own check)."""
    aborts: dict = {}
    by_new: dict[str, list[str]] = {}
    for old in sorted(protected):
        new = key_map.new_key(old)
        if new is None:
            _abort(aborts, "unmapped_key", old)
            continue
        by_new.setdefault(new, []).append(old)
    for olds in by_new.values():
        if len(olds) > 1:
            for old in olds:
                _abort(aborts, "protected_collision", old)
    if aborts:
        return set(), _sorted_aborts(aborts)
    return set(by_new), {}


def remap_writeback(records: list[tuple[str, str, str]], ledger: list[dict], key_map: KeyMap,
                    survivors: dict[str, str]) -> tuple[list[tuple[str, str, str]], dict]:
    """(alias_writeback records moved to the survivor, aborts). A record's variation_id is a copy's Medusa id or, when
    only the base was loaded, its old Key. Either becomes the survivor's id, or the new Key for a variety with no
    Medusa record. One (id, alias) pair is kept once, case-insensitively; the record kept is the first in a fixed
    order, so the result does not depend on the file's order. An id found nowhere aborts."""
    key_of_id = {r["medusa_id"]: r["key"] for r in ledger if r.get("medusa_id")}
    aborts: dict = {}
    candidates = []
    for vid, alias, method in records:
        old = key_of_id.get(vid, vid)
        new = key_map.new_key(old)
        if new is None:
            _abort(aborts, "unmapped_variation_id", vid)
            continue
        candidates.append((survivors.get(old, new), alias, method))
    out, seen = [], set()
    for vid, alias, method in sorted(candidates, key=lambda c: (c[0], c[1].casefold(), c[1], c[2])):
        if (vid, alias.casefold()) in seen:
            continue
        seen.add((vid, alias.casefold()))
        out.append((vid, alias, method))
    return out, _sorted_aborts(aborts)
