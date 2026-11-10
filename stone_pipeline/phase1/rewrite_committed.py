"""SC-4: rewrite the repo's committed variety data to phase 1's Keys (plan SC-3-SC-4-PLAN.md sections 1b and 2g).

Every committed old Key maps through SC-1's key map by its old Key, never re-minted: SC-1 read exactly these files.
The map is read once, by its VersionId, and only at its sha256 (`migrate_stores.read_key_map`, the one reader SC-3
uses too).

The variety backbone (`catalog_source/backbone_varieties.json`, spec rev 8 section 3 "Backbone result", D7): ONE
post per (type, name), merged from the three per-category backbones.
- A keyed post (every slab and tile post, 327 block posts) joins by its old Key through the map. A keyless block
  post joins its identity by (type, name). Anything else refuses: nothing is minted (measured 10 Nov: all 12,159
  block posts map, 327 by Key and 11,832 by (type, name)).
- The post: the default form's copy's variant and type, the slab post's finishes and qualities (D7), and the
  union of every copy's colours, aliases (the default form's casing first; another copy's differing name joins
  them) and merged_from. category, image_file, exists, in_csv and variant_id are dropped.
- MAPPED_BACKBONE_KEYS_SHA256 is the digest of the sorted Keys this wrote, so CI (which has no map) pins it.

The additions (`catalog_source/backbone_additions/varieties.json`): the three per-category files merged the same
way, each post carrying the sorted pack category names its copies sat in (`categories`). An untyped post has no
new Key: it stays, with key "" and stone_type "", HELD (loaders.backbone.read_posts skips it).

The base files (`from_medusa/<env>/variants_export_base.csv`): ONE row per new Key, in the four variety-file
columns (`medusa_files.VARIETY_FILE_COLUMNS`), sorted by Key.
- The Name is the default form's copy's (the lowest category rank in the map: slab, then block, then tile).
- Type is the map's canonical type name.
- Aliases are the case-insensitive union of every copy's aliases, the default form's casing first (spec rev 8
  section 3, Aliases).
- Image and Volume are dropped.

Deterministic and idempotent: the same map and files give the same bytes, and a row already on a new Key passes
through unchanged, so a second run writes 0 changed bytes. Any other Key (neither an old Key in the map nor a new
Key of it) refuses, naming it.

Run once, by hand, with the repo as the working directory: `python -m stone_pipeline.phase1.rewrite_committed`.
It reads S3 once and writes only the committed files listed in BASE_FILES.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from stone_pipeline.phase1.migrate_stores import KeyMap, read_key_map
from stone_pipeline.reference.medusa_files import VARIETY_FILE_COLUMNS, read_variants_export

# --- config ----------------------------------------------------------------------------------------------------------
KEY_MAP_BUCKET = "blokport-dev-staging-3e58a6"
KEY_MAP_KEY = "release-input/phase1/key-map.csv"
KEY_MAP_VERSION_ID = "Pl38LiTQerxFska9vMP3gbpEcB0RXAuJ"
KEY_MAP_SHA256 = "f0e18346c7ae9fd5e0817d9d0c896f483a5a9f39bc78d2d100becec4bd66ee68"
REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "catalog_source"
BACKBONE_FILES = {"slab": CATALOG / "backbone_slabs.json", "block": CATALOG / "backbone_blocks.json",
                  "tile": CATALOG / "backbone_tiles.json"}
ADDITIONS_FILES = {name: CATALOG / "backbone_additions" / f"{name}.json" for name in ("slab", "block", "tile")}
VARIETIES_FILE = CATALOG / "backbone_varieties.json"
ADDITIONS_FILE = CATALOG / "backbone_additions" / "varieties.json"
DELETED_FILES = (CATALOG / "image_model.csv", CATALOG / "missing_variants.csv")
# sha256 of the sorted Keys of the backbone posts this wrote, every one through the map: CI pins it
MAPPED_BACKBONE_KEYS_SHA256 = "ba047a4756485c3d81e8114282bb3e15d641c0f0d5e8a6e92316f16834748f7c"
BASE_FILES = (
    REPO_ROOT / "from_medusa" / "development" / "variants_export_base.csv",
    REPO_ROOT / "from_medusa" / "production" / "variants_export_base.csv",
)


class UnmappedKey(ValueError):
    """A committed Key that is neither an old Key in the map nor one of its new Keys."""


def rewrite_base(text: str, key_map: KeyMap, label: str) -> str:
    """A base file's text (either shape: the old five columns, or already the four) rewritten to one row per new
    Key, as the module docstring says."""
    new_keys = {row[0] for row in key_map.rows.values()}
    groups: dict[str, list] = {}
    records = (read_variants_export(text, require_status=False, name=label) if _has_type(text)
               else _old_records(text, label))
    for record in records:
        if key_map.new_key(record.key):
            groups.setdefault(key_map.new_key(record.key), []).append(
                (key_map.rank(record.key), record.key, record.name, key_map.type_name(record.key), record.aliases))
        elif record.key in new_keys:
            groups.setdefault(record.key, []).append((-1, record.key, record.name, record.type, record.aliases))
        else:
            raise UnmappedKey(f"{label}: Key {record.key!r} is not in key map {KEY_MAP_SHA256[:8]}")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=VARIETY_FILE_COLUMNS)
    writer.writeheader()
    for new_key in sorted(groups):
        copies = sorted(groups[new_key])                       # default form first, then by old Key
        writer.writerow({"Key": new_key, "Name": copies[0][2], "Type": copies[0][3],
                         "Aliases": "|".join(_alias_union(c[4] for c in copies))})
    return out.getvalue()


POST_FIELDS = ("key", "variant", "stone_type", "color", "finishes", "qualities", "aliases", "merged_from")


def _norm(text: str | None) -> str:
    return (text or "").strip().casefold()


class _Join:
    """Where a post's new Key comes from: its old Key through the map, an already-new Key, or (keyless) its
    identity's Key from a keyed post."""

    def __init__(self, key_map: KeyMap):
        self.key_map = key_map
        self.new_keys = {row[0] for row in key_map.rows.values()}
        self.by_identity: dict[tuple[str, str], str] = {}

    def keyed(self, post: dict, label: str) -> str | None:
        key = (post.get("key") or "").strip()
        if not key:
            return None
        found = self.key_map.new_key(key) or (key if key in self.new_keys else None)
        if found is None:
            raise UnmappedKey(f"{label}: Key {key!r} ({post.get('variant')!r}) is not in key map {KEY_MAP_SHA256[:8]}")
        self.by_identity.setdefault((_norm(post.get("stone_type")), _norm(post.get("variant"))), found)
        return found

    def keyless(self, post: dict, label: str) -> str:
        found = self.by_identity.get((_norm(post.get("stone_type")), _norm(post.get("variant"))))
        if found is None:
            raise UnmappedKey(f"{label}: keyless post {post.get('variant')!r} ({post.get('stone_type')!r}) joins no "
                              f"identity of key map {KEY_MAP_SHA256[:8]}")
        return found


def _merge_into(merged: dict, key: str, post: dict, rank: int) -> None:
    """Fold one copy into its identity's post; `rank` orders the copies (the default form's first)."""
    merged.setdefault(key, []).append((rank, post))


def _merged_post(key: str, copies: list) -> dict:
    copies = [post for _, post in sorted(copies, key=lambda c: c[0])]
    first = copies[0]
    names = [first.get("variant") or ""]
    out = {"key": key, "variant": names[0], "stone_type": first.get("stone_type") or "",
           "color": _union(c.get("color") or [] for c in copies),
           "finishes": list(first.get("finishes") or []), "qualities": list(first.get("qualities") or []),
           "aliases": [a for a in _alias_union([list(c.get("aliases") or []) + [c.get("variant") or ""]
                                                for c in copies]) if a and _norm(a) != _norm(names[0])],
           "merged_from": _union(c.get("merged_from") or [] for c in copies)}
    return {field: out[field] for field in POST_FIELDS}


def _union(lists) -> list:
    seen: list = []
    for values in lists:
        for value in values:
            if value not in seen:
                seen.append(value)
    return seen


def merge_backbones(slabs: list[dict], blocks: list[dict], tiles: list[dict], key_map: KeyMap) -> list[dict]:
    """The three per-category backbones (or an already-merged one passed as `slabs`) merged to one post per
    identity, sorted by Key (the module docstring)."""
    join, merged = _Join(key_map), {}
    for label, rank, posts in (("slab", 0, slabs), ("tile", 2, tiles), ("block", 1, blocks)):
        for post in posts:                                  # blocks last: a keyless one joins a keyed identity
            key = join.keyed(post, label) or join.keyless(post, label)
            _merge_into(merged, key, post, rank)
    return [_merged_post(key, merged[key]) for key in sorted(merged)]


def merge_additions(files: dict[str, list[dict]], key_map: KeyMap) -> list[dict]:
    """The per-category additions (category name -> posts, or {"": the merged file}) merged to one post per
    identity, each with its sorted `categories`; an untyped post (no new Key) is kept HELD with key ""."""
    join, merged, categories = _Join(key_map), {}, {}
    held: dict[str, dict] = {}
    for name, posts in files.items():
        for post in posts:
            old = (post.get("key") or "").strip()
            if old and not (key_map.new_key(old) or old in join.new_keys) and not (post.get("stone_type") or ""):
                identity = _norm(post.get("variant"))                     # untyped: no new Key, held
                held.setdefault(identity, {"posts": [], "categories": set()})
                held[identity]["posts"].append((0, post))
                held[identity]["categories"] |= set(post.get("categories") or [name])
                continue
            if old == "" and not (post.get("stone_type") or ""):           # already held (a re-run)
                identity = _norm(post.get("variant"))
                held.setdefault(identity, {"posts": [], "categories": set()})
                held[identity]["posts"].append((0, post))
                held[identity]["categories"] |= set(post.get("categories") or [])
                continue
            key = join.keyed(post, f"additions/{name}") or join.keyless(post, f"additions/{name}")
            _merge_into(merged, key, post, key_map.rank(old) if key_map.new_key(old) else 0)
            categories.setdefault(key, set()).update(post.get("categories") or [name])
    out = [{**_merged_post(key, merged[key]), "categories": sorted(categories[key])} for key in sorted(merged)]
    for identity in sorted(held):
        post = _merged_post("", held[identity]["posts"])
        out.append({**post, "stone_type": "", "categories": sorted(held[identity]["categories"])})
    return out


def _alias_union(alias_lists) -> list[str]:
    """Every alias once, case-insensitively, the first casing met kept (the default form's copy comes first)."""
    seen: dict[str, str] = {}
    for aliases in alias_lists:
        for alias in aliases:
            seen.setdefault(alias.casefold(), alias)
    return list(seen.values())


def _has_type(text: str) -> bool:
    return "Type" in next(csv.reader(io.StringIO(text.removeprefix("﻿"))), [])


class _OldRecord:
    """A row of the pre-phase-1 base (Key, Name, Image, Aliases, Volume): the export reader requires Type now, so
    the old shape is read here, the one place that still has to."""

    def __init__(self, key: str, name: str, aliases: tuple[str, ...]):
        self.key, self.name, self.aliases = key, name, aliases


def _old_records(text: str, label: str) -> list[_OldRecord]:
    reader = csv.DictReader(io.StringIO(text.removeprefix("﻿")))
    missing = [c for c in ("Key", "Name", "Aliases") if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"{label}: missing column(s) {', '.join(missing)}")
    return [_OldRecord(r["Key"].strip(), r["Name"].strip(),
                       tuple(a.strip() for a in (r["Aliases"] or "").split("|") if a.strip()))
            for r in reader if (r["Key"] or "").strip()]


def fetch_key_map() -> KeyMap:
    """The key map: ONE S3 read, by its VersionId, sha-checked by read_key_map."""
    from stone_pipeline.io import storage
    client = storage.aws_client("s3", config=storage.BOTOCORE_DEFAULTS)
    body = client.get_object(Bucket=KEY_MAP_BUCKET, Key=KEY_MAP_KEY, VersionId=KEY_MAP_VERSION_ID)["Body"].read()
    return read_key_map(body, KEY_MAP_SHA256)


def _write_if_changed(path: Path, data: bytes, changed: dict) -> None:
    before = path.read_bytes() if path.exists() else None
    changed[str(path.relative_to(REPO_ROOT))] = data != before
    if data != before:
        path.write_bytes(data)


def _read_posts(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc["posts"] if isinstance(doc, dict) else doc


def _json(doc) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def mapped_keys_sha256(posts: list[dict]) -> str:
    """The digest MAPPED_BACKBONE_KEYS_SHA256 pins: the sorted Keys of every post."""
    return hashlib.sha256("\n".join(sorted(p["key"] for p in posts)).encode()).hexdigest()


def run_backbones(key_map: KeyMap) -> tuple[dict[str, bool], str]:
    """Write the merged variety backbone and the merged additions, delete the per-category files they replace
    and the two dropped catalog files; ({file: changed}, the mapped-Keys digest to pin). A merged-in copy's name
    joins the aliases as written; `python -m stone_pipeline.reference.clean_committed_names` then makes the files
    fixed points of the name rules (10 Nov: one alias re-cased), and a second run of this changes nothing."""
    changed: dict[str, bool] = {}
    if VARIETIES_FILE.exists():
        posts = merge_backbones(_read_posts(VARIETIES_FILE), [], [], key_map)
    else:
        posts = merge_backbones(*(_read_posts(BACKBONE_FILES[c]) for c in ("slab", "block", "tile")), key_map)
    _write_if_changed(VARIETIES_FILE, _json({"posts": posts}), changed)
    sources = ({"": _read_posts(ADDITIONS_FILE)} if ADDITIONS_FILE.exists()
               else {name: _read_posts(path) for name, path in ADDITIONS_FILES.items()})
    _write_if_changed(ADDITIONS_FILE, _json(merge_additions(sources, key_map)), changed)
    for path in (*BACKBONE_FILES.values(), *ADDITIONS_FILES.values(), *DELETED_FILES):
        changed[str(path.relative_to(REPO_ROOT))] = path.exists()
        if path.exists():
            path.unlink()
    return changed, mapped_keys_sha256(posts)


def run(key_map: KeyMap) -> dict[str, bool]:
    """Rewrite each committed base in place; {file: changed}."""
    changed = {}
    for path in BASE_FILES:
        before = path.read_bytes()
        after = rewrite_base(before.decode("utf-8-sig"), key_map, str(path.relative_to(REPO_ROOT))).encode("utf-8")
        changed[str(path.relative_to(REPO_ROOT))] = after != before
        if after != before:
            path.write_bytes(after)
    return changed


if __name__ == "__main__":
    _map = fetch_key_map()
    print(run(_map))
    print(run_backbones(_map))
