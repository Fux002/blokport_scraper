"""Phase 1 SC-3, the one-off run on dev (RUN_MODE=phase1-migrate; plan SC-3-SC-4-PLAN.md sections 1, 1c, 1d, 4).

    python -m stone_pipeline.phase1.migrate_run

Inputs come from the environment, never flags:
- MIGRATE_INPUTS: one JSON object. The two maps and the new export as {key, version_id, sha256}; the env's committed
  retired-Keys file (retired_keys/{env}.csv) as {sha256}. The step-1 record:
  every store's version_id and, for the real run, the dry run's digest:
  {"key_map": {...}, "survivor_map": {...}, "export": {...}, "retired_keys": {"sha256": "..."},
   "stores": {"ledger": {"version_id": "...", "digest": "..."}, "config": ..., "writeback": ..., "refreshed": ...,
              "base_from_medusa": ..., "base_to_upload": ..., "outputs": {"version_id": "..."}}}
  A store absent at step 1 has version_id null.
- MIGRATE_WRITE: "0" is the dry run: it reads everything, decides everything and writes nothing (it prints the
  report, with each store's digest). "1" is the real run: it refuses unless each store's digest equals the dry run's.

Every store is read BY its recorded VersionId. A store whose latest version is not the recorded one is refused unless
its content is the same (a same-content re-upload passes). Any abort stops the run before its first write. The real
run writes, in order: the icon copies (add-only), the three `.v2` stores (an existing `.v2` with the same content is
"unchanged", one with other content is refused), both base copies, the best-model marker, and last wipes
outputs.tar.gz. It never writes an old store key. The report is printed as one JSON line. No em dashes.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from stone_pipeline.config.settings import ENV_NAME, ENV_SEGMENT, SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.io import storage
from stone_pipeline.io.variety_icons import VARIATIONS_PREFIX
from stone_pipeline.ledger import snapshot
from stone_pipeline.phase1 import store_writer
from stone_pipeline.phase1.migrate_stores import (MigrationRefused, read_key_map, read_retired_keys,
                                                  read_survivor_map)
from stone_pipeline.phase1.rewrite_committed import rewrite_base
from stone_pipeline.reference.medusa_files import read_variants_export
from stone_pipeline.refresh_images import MARKER_KEY
from stone_pipeline.state.writeback import _write_records, load_alias_writeback_records

log = logfmt.get_logger("phase1.migrate_run")

# --- config ---------------------------------------------------------------------------------------------------------
INPUTS_ENV, WRITE_ENV = "MIGRATE_INPUTS", "MIGRATE_WRITE"
_MISSING = ("NoSuchKey", "404", "NotFound")
# the step-1 stores: name -> (old S3 key, kind). sqlite stores digest by content, the rest by bytes.
STORES = {
    "ledger": (snapshot._legacy_key(snapshot.snapshot_key(ENV_NAME)), "sqlite"),
    "config": (snapshot._legacy_key(snapshot.config_key(ENV_NAME)), "sqlite"),
    "writeback": (snapshot._legacy_key(snapshot.state_writeback_key(ENV_NAME)), "bytes"),
    "refreshed": (MARKER_KEY, "bytes"),
    "base_from_medusa": (f"{ENV_SEGMENT}/scraper/from_medusa/variants_export_base.csv", "bytes"),
    "base_to_upload": (f"{ENV_SEGMENT}/scraper/to_upload/variants_export_base.csv", "bytes"),
    "outputs": (snapshot.artifacts_key("outputs", ENV_NAME), "bytes"),
}
# the `.v2` stores: name -> new S3 key (code constants, snapshot.py)
NEW_STORES = {"ledger": snapshot.snapshot_key(ENV_NAME), "config": snapshot.config_key(ENV_NAME),
              "writeback": snapshot.state_writeback_key(ENV_NAME)}
REQUIRED_STORES = ("ledger", "config")     # the migration has nothing to migrate without them
# the env's committed retired-Keys file (baked into the image), read only at MIGRATE_INPUTS.retired_keys.sha256
RETIRED_KEYS_DIR = Path(__file__).with_name("retired_keys")
RETIRED_KEYS_PATH = RETIRED_KEYS_DIR / f"{ENV_NAME}.csv"


class _S3:
    """The calls the run makes, on one client, every one by VersionId when it reads a step-1 object."""

    def __init__(self, client, bucket: str):
        self.c, self.bucket = client, bucket

    def get(self, key: str, version_id: str) -> bytes:
        return self.c.get_object(Bucket=self.bucket, Key=key, VersionId=version_id)["Body"].read()

    def latest(self, key: str) -> str | None:
        """The latest VersionId of `key`, or None when it does not exist; any other error raises."""
        try:
            return self.c.head_object(Bucket=self.bucket, Key=key)["VersionId"]
        except Exception as exc:
            if getattr(exc, "response", {}).get("Error", {}).get("Code") in _MISSING:
                return None
            raise

    def listing(self, prefix: str) -> dict[str, str]:
        out = {}
        for page in self.c.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                out[obj["Key"]] = (obj.get("ETag") or "").strip('"')
        return out

    def put(self, key: str, path: Path) -> str:
        with path.open("rb") as h:
            return self.c.put_object(Bucket=self.bucket, Key=key, Body=h)["VersionId"]

    def copy(self, source: str, destination: str) -> str:
        out = self.c.copy_object(Bucket=self.bucket, Key=destination, CopySource={"Bucket": self.bucket, "Key": source})
        return out["CopyObjectResult"]["ETag"].strip('"')

    def delete(self, key: str) -> None:
        self.c.delete_object(Bucket=self.bucket, Key=key)


def _digest(path: Path, kind: str) -> str:
    return store_writer.sqlite_digest(path) if kind == "sqlite" else store_writer.bytes_digest(path.read_bytes())


def _versioned(s3: _S3, spec: dict, what: str) -> bytes:
    data = s3.get(spec["key"], spec["version_id"])
    if store_writer.bytes_digest(data) != spec["sha256"]:
        raise MigrationRefused(f"{what}: {spec['key']}@{spec['version_id']} is not sha256 {spec['sha256']}")
    return data


def _retired_keys_pin(inputs: dict) -> str:
    """MIGRATE_INPUTS.retired_keys.sha256, the only digest the env's committed retired-Keys file is read at; an absent
    or empty pin refuses by name, before any store is read."""
    spec = inputs.get("retired_keys")
    sha = spec.get("sha256") if isinstance(spec, dict) else None
    if not isinstance(sha, str) or not sha:
        raise MigrationRefused("MIGRATE_INPUTS has no retired_keys {sha256} pin: the env's committed retired-Keys file "
                               f"({RETIRED_KEYS_PATH.name}) is read only at its pinned digest")
    return sha


def _committed(path: Path, what: str) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise MigrationRefused(f"{what}: {path} is not committed for this env") from exc


def _fetch_stores(s3: _S3, recorded: dict, work: Path, write: bool) -> dict:
    """Each step-1 store, read by its recorded VersionId into `work`: name -> {path, digest, version_id}. Refuses a
    store moved on since step 1 with other content, and in the real run a digest that is not the dry run's."""
    out = {}
    for name, (key, kind) in STORES.items():
        spec = recorded[name]
        latest = s3.latest(key)
        if spec["version_id"] is None:
            if name in REQUIRED_STORES:
                raise MigrationRefused(f"{name}: a step-1 version is required")
            if latest is not None:
                raise MigrationRefused(f"{name}: {key} was absent at step 1 and exists now ({latest})")
            out[name] = {"path": None, "digest": None, "version_id": None}
            continue
        path = work / f"step1-{name}"
        path.write_bytes(s3.get(key, spec["version_id"]))
        digest = _digest(path, kind)
        if latest != spec["version_id"]:
            if latest is None:
                raise MigrationRefused(f"{name}: {key} existed at step 1 ({spec['version_id']}) and is gone now")
            now = work / f"latest-{name}"
            now.write_bytes(s3.get(key, latest))
            if _digest(now, kind) != digest:
                raise MigrationRefused(f"{name}: {key} moved on since step 1 ({spec['version_id']} -> {latest}) "
                                       "with other content")
        if write and spec.get("digest") != digest:
            raise MigrationRefused(f"{name}: digest {digest} is not the dry run's {spec.get('digest')}")
        out[name] = {"path": path, "digest": digest, "version_id": spec["version_id"]}
    return out


def _plan(s3: _S3, inputs: dict, stores: dict, work: Path,
          retired_pin: str) -> tuple[dict, store_writer.Report, dict]:
    """Every decision and every new file, written under `work`, nothing to S3: (new files by name, report, the
    icon copies)."""
    key_map = read_key_map(_versioned(s3, inputs["key_map"], "key map"), inputs["key_map"]["sha256"])
    survivors = read_survivor_map(_versioned(s3, inputs["survivor_map"], "survivor map"),
                                  inputs["survivor_map"]["sha256"], inputs["key_map"]["sha256"])
    retired_keys = read_retired_keys(_committed(RETIRED_KEYS_PATH, "retired keys"), retired_pin, key_map)
    export = read_variants_export(_versioned(s3, inputs["export"], "export").decode("utf-8-sig"),
                                  require_status=True, name=inputs["export"]["key"])
    archived = {v.key for v in export if v.status == "archived"}
    report, files = store_writer.Report(), {}

    icons = store_writer.plan_icon_copies(s3.listing(VARIATIONS_PREFIX), key_map)
    _fold(report, icons.report)
    old_rows = store_writer.ledger_variation_rows(stores["ledger"]["path"])
    files["ledger"] = work / "ledger.v2.db"
    _fold(report, store_writer.build_ledger(stores["ledger"]["path"], files["ledger"], key_map, survivors, archived,
                                            icons.pairs))
    files["config"] = work / "config.v2.db"
    _fold(report, store_writer.migrate_config(stores["config"]["path"], files["config"], key_map,
                                              {r["key"] for r in old_rows}))
    records = load_alias_writeback_records(stores["writeback"]["path"]) if stores["writeback"]["path"] else []
    moved, sub = store_writer.migrate_writeback(records, old_rows, key_map, survivors)
    _fold(report, sub)
    files["writeback"] = work / "alias_writeback.v2.csv"
    _write_records(files["writeback"], moved)
    for name in ("base_from_medusa", "base_to_upload"):
        if stores[name]["path"] is None:
            continue
        files[name] = work / f"{name}.csv"
        files[name].write_text(rewrite_base(stores[name]["path"].read_text(encoding="utf-8-sig"), key_map,
                                            STORES[name][0]), encoding="utf-8")
    if stores["refreshed"]["path"] is not None:
        old_keys = store_writer.old_marker_keys(stores["refreshed"]["path"].read_bytes())
        pairs, sub = store_writer.remap_marker(old_keys, key_map,
                                               store_writer.ledger_variation_tombstones(stores["ledger"]["path"]),
                                               retired_keys)
        _fold(report, sub)
        files["refreshed"] = work / "refreshed.json"
        files["refreshed"].write_text(json.dumps(pairs), encoding="utf-8")
    report.counts["survivors_expected"] = len(set(survivors.values()))
    return files, report, icons.copies


def _fold(into: store_writer.Report, part: store_writer.Report) -> None:
    into.counts.update(part.counts)
    into.listed.update(part.listed)
    into.merge_aborts(part.aborts)


def _write_new_store(s3: _S3, name: str, path: Path, out: dict) -> None:
    """A `.v2` store: put when absent; an existing one with the same content is unchanged; other content refuses."""
    key, kind = NEW_STORES[name], STORES[name][1]
    existing = s3.latest(key)
    digest = _digest(path, kind)
    if existing is not None:
        there = path.with_suffix(".existing")
        there.write_bytes(s3.get(key, existing))
        if _digest(there, kind) != digest:
            raise MigrationRefused(f"{key} already exists ({existing}) with other content")
        out[name] = {"key": key, "version_id": existing, "digest": digest, "unchanged": True}
        return
    out[name] = {"key": key, "version_id": s3.put(key, path), "digest": digest, "unchanged": False}


def _write(s3: _S3, files: dict, copies: list, stores: dict) -> dict:
    """The real run's writes, in the documented order; returns what was written, with every VersionId."""
    out = {"icons_copied": 0}
    for source, destination, etag in copies:
        if s3.copy(source, destination) != etag:
            raise MigrationRefused(f"icon copy {source} -> {destination}: the copy's ETag is not the source's")
        out["icons_copied"] += 1
    for name in NEW_STORES:
        _write_new_store(s3, name, files[name], out)
    for name in ("base_from_medusa", "base_to_upload", "refreshed"):
        if name in files:
            out[name] = {"key": STORES[name][0], "version_before": stores[name]["version_id"],
                         "version_after": s3.put(STORES[name][0], files[name])}
    if stores["outputs"]["version_id"] is not None:
        s3.delete(STORES["outputs"][0])
        out["outputs"] = {"key": STORES["outputs"][0], "version_before": stores["outputs"]["version_id"],
                          "wiped": True}
    return out


def run(client, inputs: dict, write: bool) -> dict:
    """The whole run against an S3 client; returns the report. Raises MigrationRefused on a refused input."""
    retired_pin = _retired_keys_pin(inputs)
    s3 = _S3(client, SETTINGS.s3.bucket)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        stores = _fetch_stores(s3, inputs["stores"], work, write)
        files, report, copies = _plan(s3, inputs, stores, work, retired_pin)
        result = {"write": write, "counts": report.counts, "listed": report.listed, "aborts": report.aborts,
                  "step1_digests": {n: s["digest"] for n, s in stores.items()}}
        if report.aborts:
            return result
        result["new_digests"] = {n: _digest(files[n], STORES[n][1]) for n in NEW_STORES}
        if not write:
            return result
        result["written"] = _write(s3, files, copies, stores)
        return result


def main() -> int:
    write = os.environ.get(WRITE_ENV, "0").strip()
    if write not in ("0", "1"):
        raise SystemExit(f"{WRITE_ENV} must be 0 (dry run) or 1 (write), not {write!r}")
    inputs = json.loads(os.environ[INPUTS_ENV])
    client = storage.aws_client("s3", config=storage.BOUNDED)
    result = run(client, inputs, write == "1")
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    if result["aborts"]:
        log.error("phase 1 store migration ABORTED: nothing was written", extra={"extra_fields": result["aborts"]})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
