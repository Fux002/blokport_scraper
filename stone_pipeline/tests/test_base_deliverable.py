"""The scraper's variety base is a listed deliverable (Blokport D7: its "restore base" import verifies every file
against the manifest). The catalog copies the base THIS produce settled (from_medusa/<env>/, after
sync_variants_base) into to_upload/<env>/ byte for byte, so the manifest lists it like 1_variants_full.csv:
sha256 of those bytes, CSV rows, uploaded only when changed. A produce that runs no catalog publishes nothing. A
factory reset publishes the seed base through the same publisher under a manifest listing only it."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from stone_pipeline.io import deliverables as ua
from stone_pipeline import catalog, produce
from stone_pipeline.config import settings
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


def test_the_copy_is_raw_bytes_never_decoded(tmp_path):
    base, deliverable = tmp_path / "base.csv", tmp_path / "out" / VARIANTS_BASE_FILE
    raw = b"Key,Name\r\nslab_marble_a_1,\xff\xfe not utf-8\r\n"
    base.write_bytes(raw)
    svb.write_deliverable(base, deliverable)
    assert deliverable.read_bytes() == raw and list(deliverable.parent.iterdir()) == [deliverable]   # no temp left


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
    monkeypatch.setattr(catalog.emit_catalog, "build", noop)
    monkeypatch.setattr(catalog, "collect_inventory", lambda root: 0)
    monkeypatch.setattr(catalog, "collect_discontinued", lambda root: 0)
    monkeypatch.setattr(catalog, "collect_lot_cards", lambda root: 0)
    monkeypatch.setattr(catalog, "write_sync_md", lambda **k: tmp_path / "SYNC_STEPS.md")
    monkeypatch.setattr(catalog.prepare_variant_images, "queue_new_textures", lambda: 0)
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
    assert entry["version_id"] == s3.versions[f"dev/scraper/to_upload/{VARIANTS_BASE_FILE}"]
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
    assert out["manifest"]["files"][VARIANTS_BASE_FILE]["version_id"] == s3.versions[key]
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
    monkeypatch.setattr(produce.build, "run", lambda sources, stage: 0)
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: None)
    monkeypatch.setattr(snapshot, "save_state", lambda *a, **k: None)
    monkeypatch.setattr(ua, "upload_all", lambda run_id=None: published.append(run_id) or 0)
    assert produce.run(None, stage) == 0
    assert published == []


def _reset_world(tmp_path, monkeypatch, s3: _S3) -> tuple[Path, Path, Path]:
    """A factory reset's base half on tmp paths: the seed, the live base, to_upload/ and one fake S3 for every
    boto3 client (the from_medusa publish and the deliverable publisher)."""
    import boto3
    seed, base, to_upload = tmp_path / "seed.csv", tmp_path / "from_medusa" / VARIANTS_BASE_FILE, tmp_path / "to_upload"
    base.parent.mkdir()
    to_upload.mkdir()
    seed.write_bytes(_BASE)
    base.write_bytes(b"Key,Name\nslab_marble_minted_7,Minted\n")                # the grown base before the reset
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    # the deployed reset writes the bucket: SCRAPER_S3_DRY_RUN=false on the dev and prod task defs
    monkeypatch.setattr(settings, "SETTINGS",
                        dataclasses.replace(settings.SETTINGS, s3=dataclasses.replace(settings.SETTINGS.s3, dry_run=False)))
    monkeypatch.setattr(svb, "DELIVERABLE", to_upload / VARIANTS_BASE_FILE)
    monkeypatch.setattr(ua, "SETTINGS", types.SimpleNamespace(paths=types.SimpleNamespace(to_upload_dir=to_upload)))
    return seed, base, to_upload


def test_a_factory_reset_publishes_a_manifest_listing_only_the_seed_base(tmp_path, monkeypatch):
    """Leftover produce files on the task and a full manifest on S3: after the reset the manifest lists the seed
    base and nothing else (the old files stay on S3, unlisted). The next produce publishes a full set again and,
    its base unchanged from the seed, does not re-upload the base."""
    from stone_pipeline import lifecycle
    from stone_pipeline.config.settings import ENV_SEGMENT
    s3 = _S3()
    seed, base, to_upload = _reset_world(tmp_path, monkeypatch, s3)
    prefix = f"{ENV_SEGMENT}/scraper/to_upload"
    (to_upload / "1_variants_full.csv").write_text("Key\nslab_marble_minted_7\n", encoding="utf-8")
    (to_upload / "3_products_all.csv").write_text("Handle\nh1\n", encoding="utf-8")
    (to_upload / "SYNC_STEPS.md").write_text("# steps\n", encoding="utf-8")         # a side file
    svb.write_deliverable(base)
    ua.publish_to_upload(s3, "before-reset")                                   # the pre-reset full set on S3

    assert lifecycle._reseed_base_from_pristine(seed_path=seed, base_path=base) == {"reseeded": True,
                                                                                    "s3_published": True}
    assert lifecycle._publish_base_deliverable(base) is True                    # the factory reset's own step
    manifest = json.loads(s3.objects[f"{prefix}/{ua.MANIFEST_NAME}"])
    assert list(manifest["files"]) == [VARIANTS_BASE_FILE] and manifest["side_files"] == {}
    entry = manifest["files"][VARIANTS_BASE_FILE]
    assert entry["sha256"] == hashlib.sha256(_BASE).hexdigest() and entry["rows"] == 2
    assert entry["version_id"] == s3.versions[f"{prefix}/{VARIANTS_BASE_FILE}"]    # the version holding the seed
    assert s3.objects[f"{prefix}/{VARIANTS_BASE_FILE}"] == _BASE              # the key holds the seed's bytes
    assert s3.objects[f"{ENV_SEGMENT}/scraper/from_medusa/{VARIANTS_BASE_FILE}"] == _BASE
    assert f"{prefix}/1_variants_full.csv" in s3.objects                       # stale, kept, no longer listed

    # the next produce settles the same base (nothing minted yet) and publishes its full folder
    s3.uploads.clear()
    svb.write_deliverable(base)
    out = ua.publish_to_upload(s3, "r2")
    assert VARIANTS_BASE_FILE in out["unchanged"] and f"{prefix}/{VARIANTS_BASE_FILE}" not in s3.uploads
    assert out["manifest"]["files"][VARIANTS_BASE_FILE] == entry
    assert set(out["manifest"]["files"]) == {VARIANTS_BASE_FILE, "1_variants_full.csv", "3_products_all.csv"}
    assert set(out["manifest"]["side_files"]) == {"SYNC_STEPS.md"}


def test_publishing_only_a_name_the_folder_lacks_writes_nothing(tmp_path):
    s3 = _S3()
    (tmp_path / "1_variants_full.csv").write_text("Key\n", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match=VARIANTS_BASE_FILE):
        ua.publish_deliverables(s3, "b", tmp_path, "p", run_id=None, only={VARIANTS_BASE_FILE})
    assert s3.objects == {}


def test_a_failed_deliverable_publish_is_reported_not_hidden(tmp_path, monkeypatch):
    from stone_pipeline import lifecycle
    s3 = _S3()
    seed, base, _ = _reset_world(tmp_path, monkeypatch, s3)

    def _down(client, run_id):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")
    monkeypatch.setattr(ua, "publish_to_upload", _down)
    assert lifecycle._reseed_base_from_pristine(seed_path=seed, base_path=base)["reseeded"]
    assert lifecycle._publish_base_deliverable(base) is False
    assert base.read_bytes() == _BASE                                           # the reseed itself still happened


def test_the_produce_publish_entry_point_lists_the_base_beside_the_side_files(tmp_path, monkeypatch):
    """deliverables.upload_all (the produce's publish) goes through publish_to_upload without `only`: the whole
    folder, the base under `files` with its version, side files under `side_files`."""
    import boto3
    from stone_pipeline.config.settings import ENV_SEGMENT
    s3 = _S3()
    to_upload, review = tmp_path / "to_upload", tmp_path / "review"
    to_upload.mkdir()
    (to_upload / VARIANTS_BASE_FILE).write_bytes(_BASE)
    (to_upload / "1_variants_full.csv").write_text("Key\nslab_marble_a_1\n", encoding="utf-8")
    (to_upload / "SYNC_STEPS.md").write_text("# steps\n", encoding="utf-8")
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(ua, "SETTINGS", types.SimpleNamespace(
        paths=types.SimpleNamespace(to_upload_dir=to_upload, review_dir=review)))
    assert ua.upload_all("r1") == 0
    m = json.loads(s3.objects[f"{ENV_SEGMENT}/scraper/to_upload/{ua.MANIFEST_NAME}"])
    assert set(m["files"]) == {VARIANTS_BASE_FILE, "1_variants_full.csv"} and set(m["side_files"]) == {"SYNC_STEPS.md"}
    assert m["files"][VARIANTS_BASE_FILE]["version_id"] == s3.versions[f"{ENV_SEGMENT}/scraper/to_upload/{VARIANTS_BASE_FILE}"]


def test_a_curation_rebuild_reseeds_the_base_but_leaves_the_published_manifest_unchanged(tmp_path, monkeypatch):
    """Medusa is not wiped by a curation repair: the published full set stays valid. It reseeds the local base and
    publishes no deliverable; the catalog run it starts publishes the full set, base included."""
    import functools
    from stone_pipeline import lifecycle
    from stone_pipeline.config.control import runner
    from stone_pipeline.config.settings import ENV_SEGMENT
    s3 = _S3()
    seed, base, to_upload = _reset_world(tmp_path, monkeypatch, s3)
    prefix = f"{ENV_SEGMENT}/scraper/to_upload"
    (to_upload / "1_variants_full.csv").write_text("Key\nslab_marble_minted_7\n", encoding="utf-8")
    svb.write_deliverable(base)
    ua.publish_to_upload(s3, "before-repair")
    published = dict(s3.objects)

    monkeypatch.setattr(lifecycle, "_reseed_base_from_pristine",
                        functools.partial(lifecycle._reseed_base_from_pristine, seed_path=seed, base_path=base))
    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: (work(None, types.SimpleNamespace(
        _lock_and_check_in_flight=lambda lg: None)), 200))
    monkeypatch.setattr(runner, "start_run", lambda sources, stage: ({"run_id": "r9"}, 202))
    out, code = lifecycle.rebuild_curation()
    assert code == 200 and out["rebuilt"] and "import_published" not in out["base_reseed"]
    assert base.read_bytes() == _BASE                                           # the local base is the seed
    assert {k: v for k, v in s3.objects.items() if k.startswith(prefix)} == \
        {k: v for k, v in published.items() if k.startswith(prefix)}           # manifest + deliverables untouched
