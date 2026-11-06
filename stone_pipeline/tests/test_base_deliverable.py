"""The scraper's variety base is a listed deliverable (Blokport D7: its "restore base" import verifies every file
against the manifest). The catalog copies the base THIS produce settled (from_medusa/<env>/, after
sync_variants_base) into to_upload/<env>/ byte for byte, so the manifest lists it like 1_variants_full.csv:
sha256 of those bytes, CSV rows, uploaded only when changed. A produce that runs no catalog publishes nothing."""

from __future__ import annotations

import hashlib
import json
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from deploy import upload_artifacts as ua
from stone_pipeline import catalog, produce
from stone_pipeline.config.settings import SETTINGS, VARIANTS_BASE_FILE
from stone_pipeline.reference import sync_variants_base as svb

# BOM, CRLF, a quoted newline and a non-ASCII header: any re-encoding or newline translation changes the bytes.
_BASE = ("﻿Key,Name,Image,Aliases,Volume per kg (m³/kg)\r\n"
         'slab_marble_a_1,Árabescato,,"A\nB",0.00037\r\n'
         "slab_granite_b_2,Bianco,,,\r\n").encode("utf-8")


class _S3:
    """A fake S3 holding objects by key; records uploads. put_object/head_object return a VersionId so the fake
    also serves a publisher that names each object's version."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, str] = {}
        self.uploads: list[str] = []
        self._writes = 0

    def _store(self, key, data):
        self._writes += 1
        self.objects[key], self.versions[key] = data, f"v{self._writes}"
        return {"VersionId": self.versions[key]}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": types.SimpleNamespace(read=lambda: self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"VersionId": self.versions[Key]}

    def upload_file(self, filename, bucket, key):
        self.uploads.append(key)
        self._store(key, Path(filename).read_bytes())

    def put_object(self, Bucket, Key, Body, **kw):
        data = Body if isinstance(Body, bytes) else Body.read()
        if not Key.endswith(ua.MANIFEST_NAME):
            self.uploads.append(Key)
        return self._store(Key, data)


def test_the_deliverable_is_the_settled_base_byte_for_byte(tmp_path):
    base, deliverable = tmp_path / "from_medusa" / VARIANTS_BASE_FILE, tmp_path / "to_upload" / VARIANTS_BASE_FILE
    base.parent.mkdir()
    base.write_bytes(_BASE)
    svb.write_deliverable(base, deliverable)
    assert deliverable.read_bytes() == _BASE


def test_a_stale_deliverable_from_an_earlier_produce_is_replaced(tmp_path):
    base, deliverable = tmp_path / "base.csv", tmp_path / "out" / VARIANTS_BASE_FILE
    deliverable.parent.mkdir()
    deliverable.write_bytes(b"Key,Name\nslab_old_gone_9,Old\n")
    base.write_bytes(_BASE)
    svb.write_deliverable(base, deliverable)
    assert deliverable.read_bytes() == _BASE


def test_an_absent_base_fails_loud_and_leaves_no_stale_deliverable(tmp_path):
    deliverable = tmp_path / VARIANTS_BASE_FILE
    deliverable.write_bytes(b"Key,Name\nslab_old_gone_9,Old\n")
    with pytest.raises(FileNotFoundError, match="no settled missing.csv"):
        svb.write_deliverable(tmp_path / "missing.csv", deliverable)
    assert not deliverable.exists()


def test_the_deliverable_name_is_the_base_file_name():
    assert SETTINGS.paths.variants_base_deliverable_csv == SETTINGS.paths.to_upload_dir / VARIANTS_BASE_FILE
    assert SETTINGS.paths.variants_export_base_csv.name == VARIANTS_BASE_FILE
    assert svb.DELIVERABLE == SETTINGS.paths.variants_base_deliverable_csv


def test_the_catalog_copies_the_base_after_the_sync_settles_it(tmp_path, monkeypatch):
    """The produce fetched one base; sync_variants_base rewrites it from 1_variants_full during the catalog. The
    deliverable is the rewritten one, written by catalog.run itself."""
    base, deliverable = tmp_path / "from_medusa" / VARIANTS_BASE_FILE, tmp_path / "to_upload" / VARIANTS_BASE_FILE
    base.parent.mkdir()
    base.write_bytes(b"Key,Name\nslab_fetched_1,Fetched\n")
    monkeypatch.setattr(svb, "BASE", base)
    monkeypatch.setattr(svb, "DELIVERABLE", deliverable)
    monkeypatch.setattr(svb, "sync", lambda: base.write_bytes(_BASE))
    order: list[str] = []
    noop = lambda *a, **k: None  # noqa: E731
    monkeypatch.setattr(catalog, "find_canonical", lambda root: [tmp_path / "x.parquet"])
    monkeypatch.setattr(catalog.staging, "read_canonical", lambda p: [])
    monkeypatch.setattr(catalog.loaders, "load_all", noop)
    monkeypatch.setattr(catalog.curate, "run", lambda rows, ref: types.SimpleNamespace(counts={}))
    monkeypatch.setattr("stone_pipeline.stages.decisions.heal_drifted_membership", noop)
    monkeypatch.setattr(catalog, "collect_products", lambda root: {})
    monkeypatch.setattr(catalog.build_tile_backbone, "build", noop)
    monkeypatch.setattr(catalog.emit_catalog, "build", noop)
    monkeypatch.setattr(catalog, "collect_inventory", lambda root: 0)
    monkeypatch.setattr(catalog, "collect_discontinued", lambda root: 0)
    monkeypatch.setattr(catalog, "collect_lot_cards", lambda root: 0)
    monkeypatch.setattr(catalog, "write_sync_md", lambda **k: tmp_path / "SYNC_STEPS.md")
    monkeypatch.setattr(catalog, "_auto_queue_images", lambda: 0)
    monkeypatch.setattr("stone_pipeline.stages.variety_color.fill_colors", lambda **k: {})
    monkeypatch.setattr(catalog, "write_variants_to_delete", lambda: 0)
    monkeypatch.setattr(catalog, "migrate_retyped_variant_images", lambda ref: 0)
    monkeypatch.setattr(catalog, "drop_deleted_variants", lambda: 0)
    monkeypatch.setattr(catalog, "prune_superseded_runs", lambda root: 0)
    monkeypatch.setattr(catalog.tree_build, "run", lambda: order.append("tree"))
    monkeypatch.setattr(catalog, "_record_catalog", lambda: order.append("record"))
    monkeypatch.setattr(catalog, "_consistency_gate", lambda: order.append("gate"))
    catalog.run(tmp_path)
    assert deliverable.read_bytes() == _BASE
    assert order == ["tree", "record", "gate"]


def _full(path: Path, keys: list[str]) -> None:
    rows = ["Key,Name,Image,Aliases,Volume per kg (m³/kg),Id"] + [f"{k},{k.title()},,,,id_{k}" for k in keys]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _produce(tmp_path: Path, keys: list[str]) -> Path:
    """One catalog's base half: base := 1_variants_full (the real sync), then the deliverable from it."""
    to_upload, base = tmp_path / "to_upload", tmp_path / "from_medusa" / VARIANTS_BASE_FILE
    to_upload.mkdir(exist_ok=True)
    base.parent.mkdir(exist_ok=True)
    _full(to_upload / "1_variants_full.csv", keys)
    svb.sync(full_path=to_upload / "1_variants_full.csv", base_path=base, publish=False)
    svb.write_deliverable(base, to_upload / VARIANTS_BASE_FILE)
    return base


def test_the_base_is_listed_in_the_manifest_files_with_its_sha256_and_rows(tmp_path):
    base = _produce(tmp_path, ["slab_marble_a_1", "slab_marble_b_2", "block_granite_c_3"])
    s3 = _S3()
    out = ua.publish_deliverables(s3, "b", tmp_path / "to_upload", "dev/scraper/to_upload", run_id="r1")
    entry = out["manifest"]["files"][VARIANTS_BASE_FILE]
    assert VARIANTS_BASE_FILE not in out["manifest"].get("side_files", {})
    assert entry["sha256"] == hashlib.sha256(base.read_bytes()).hexdigest()
    assert entry["rows"] == 3 and entry["bytes"] == base.stat().st_size
    assert s3.objects[f"dev/scraper/to_upload/{VARIANTS_BASE_FILE}"] == base.read_bytes()
    assert json.loads(s3.objects["dev/scraper/to_upload/manifest.json"])["files"][VARIANTS_BASE_FILE] == entry


def test_an_unchanged_base_is_not_re_uploaded_and_a_changed_one_is(tmp_path):
    s3, key = _S3(), f"p/{VARIANTS_BASE_FILE}"
    _produce(tmp_path, ["slab_marble_a_1", "slab_marble_b_2"])
    ua.publish_deliverables(s3, "b", tmp_path / "to_upload", "p", run_id="r1")
    s3.uploads.clear()
    _produce(tmp_path, ["slab_marble_a_1", "slab_marble_b_2"])          # the same base again
    out = ua.publish_deliverables(s3, "b", tmp_path / "to_upload", "p", run_id="r2")
    assert VARIANTS_BASE_FILE in out["unchanged"] and key not in s3.uploads
    base = _produce(tmp_path, ["slab_marble_a_1", "slab_marble_b_2", "slab_onyx_c_3"])   # a mint grows it
    out = ua.publish_deliverables(s3, "b", tmp_path / "to_upload", "p", run_id="r3")
    assert VARIANTS_BASE_FILE in out["uploaded"] and s3.objects[key] == base.read_bytes()
    assert out["manifest"]["files"][VARIANTS_BASE_FILE]["rows"] == 3


@pytest.mark.parametrize("stage", ["inventory", "scrape"])
def test_a_produce_without_a_catalog_publishes_nothing(stage, monkeypatch):
    """A produce that runs no catalog settles no base and builds no deliverables, so it publishes no manifest: the
    one on S3 keeps naming the last catalog produce's base with the files of that same produce."""
    from stone_pipeline.ledger import snapshot
    published: list = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: None)
    monkeypatch.setattr(produce, "_live_scrape", lambda s: 0)
    monkeypatch.setattr(produce, "_finalize_control_plane", lambda stage: None)
    monkeypatch.setattr(produce.build, "main", lambda argv: 0)
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(snapshot, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(ua, "main", lambda run_id=None: published.append(run_id) or 0)
    assert produce.main(["--stage", stage]) == 0
    assert published == []
