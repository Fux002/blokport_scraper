"""SC-4: rewrite the repo's committed variety data to phase 1's Keys (plan SC-3-SC-4-PLAN.md sections 1b and 2g).

Every committed old Key maps through SC-1's key map by its old Key, never re-minted: SC-1 read exactly these files.
The map is read once, by its VersionId, and only at its sha256 (`migrate_stores.read_key_map`, the one reader SC-3
uses too).

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
import io
from pathlib import Path

from stone_pipeline.phase1.migrate_stores import KeyMap, read_key_map
from stone_pipeline.reference.medusa_files import VARIETY_FILE_COLUMNS, read_variants_export

# --- config ----------------------------------------------------------------------------------------------------------
KEY_MAP_BUCKET = "blokport-dev-staging-3e58a6"
KEY_MAP_KEY = "release-input/phase1/key-map.csv"
KEY_MAP_VERSION_ID = "Pl38LiTQerxFska9vMP3gbpEcB0RXAuJ"
KEY_MAP_SHA256 = "f0e18346c7ae9fd5e0817d9d0c896f483a5a9f39bc78d2d100becec4bd66ee68"
REPO_ROOT = Path(__file__).resolve().parents[2]
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
    print(run(fetch_key_map()))
