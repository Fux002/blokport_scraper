"""deploy.mark_logo_step_results: at the deploy that retired the logo step, every result the step made (RUN_TAG, a source
watermarked in a pre-migration config.db snapshot) gets its logo-step marker, so the photo process never completes it
nor makes its close-up again without that step. DRY_RUN is explicit, a migrated snapshot is refused, and a second run
marks nothing."""
from __future__ import annotations

import io
import json
import sqlite3

import pytest

from deploy import mark_logo_step_results as m
from deploy.standardise_source import logo_step_key, round_prefix
from stone_pipeline.config.settings import S3_BUCKET, STANDARDISE as C

A, B, D, E = "a" * 64, "b" * 64, "d" * 64, "e" * 64


def _config_db(tmp_path, rows, migrated=False):
    path = tmp_path / f"cfg-{len(list(tmp_path.iterdir()))}.db"
    with sqlite3.connect(path) as conn:
        flag = "image_pipeline" if migrated else "watermarked"
        conn.execute(f"CREATE TABLE source (source TEXT PRIMARY KEY, {flag} INTEGER NOT NULL DEFAULT 0)")
        conn.executemany(f"INSERT INTO source (source, {flag}) VALUES (?, ?)", rows)
    return path.read_bytes()


class _S3:
    def __init__(self, objects):
        self.objects = dict(objects)            # (bucket, key, version or None) -> bytes
        self.written = []

    def _keys(self, bucket):
        return sorted(k for b, k, v in self.objects if b == bucket and v is None)

    def get_object(self, Bucket, Key, VersionId=None):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key, VersionId)])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key, None)] = Body
        self.written.append(Key)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {"KeyCount": sum(k.startswith(Prefix) for k in self._keys(Bucket))}

    def get_paginator(self, _name):
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in outer._keys(Bucket) if k.startswith(Prefix)]}
        return _P()


def _results(src, tag=C.RUN_TAG):
    out = round_prefix(tag, src)
    return out, {
        f"{out}{A}.main.jpg": b"", f"{out}{A}.studio.jpg": b"",                               # outdated open book
        f"{out}{B}.main.jpg": b"", f"{out}{B}.studio.jpg": b"", f"{out}{B}.slab.png": b"",     # complete
        f"{out}{D}.main.jpg": b"", f"{out}{D}.studio.jpg": b"", f"{out}noslab/{D}.json": b"",  # tried already
        f"{out}{E}.main.jpg": b"",                                                             # no side view: no book
    }


@pytest.fixture
def bucket(tmp_path):
    objects = {}
    for src, tag in (("varsha", C.RUN_TAG), ("varsha", C.CLEAN_TAG), ("sentas", C.RUN_TAG)):
        objects.update({(S3_BUCKET, k, None): v for k, v in _results(src, tag)[1].items()})
    objects[("prod-bucket", "production/scraper/config/config.db", "v1")] = _config_db(tmp_path, [("varsha", 1), ("sentas", 0)])
    objects[("dev-bucket", "development/scraper/config/config.db", None)] = _config_db(tmp_path, [("varsha", 0)])
    objects[("dev-bucket", "development/scraper/config/config.db", "v9")] = _config_db(tmp_path, [("varsha", 1)], migrated=True)
    return _S3(objects)


def test_dry_run_must_be_stated(monkeypatch):
    for value in (None, "", "yes", "1"):
        if value is None:
            monkeypatch.delenv("DRY_RUN", raising=False)
        else:
            monkeypatch.setenv("DRY_RUN", value)
        with pytest.raises(ValueError, match="DRY_RUN"):
            m.dry_run()
    monkeypatch.setenv("DRY_RUN", "false")
    assert m.dry_run() is False


def test_the_evidence_is_the_union_of_pre_migration_snapshots_and_a_migrated_one_is_refused(bucket, monkeypatch):
    monkeypatch.setenv("CONFIG_DB_SNAPSHOTS", "dev-bucket/development/scraper/config/config.db,"
                                              "prod-bucket/production/scraper/config/config.db@v1")
    assert m.evidence(bucket) == {"varsha"}                 # dev's own says no; prod's run made dev's results
    monkeypatch.setenv("CONFIG_DB_SNAPSHOTS", "dev-bucket/development/scraper/config/config.db@v9")
    with pytest.raises(ValueError, match="already migrated"):
        m.evidence(bucket)
    monkeypatch.setenv("CONFIG_DB_SNAPSHOTS", "no-key-here")
    with pytest.raises(ValueError, match="<bucket>/<key>"):
        m.snapshots()


def test_every_run_tag_result_of_the_marked_sources_is_marked_once(bucket):
    every = [A, B, D, E]                                     # outdated, completed, tried, a close-up: each a result
    assert m.mark(bucket, {"varsha"}, write=False) == {"varsha": every} and bucket.written == []   # dry run: nothing
    assert m.mark(bucket, {"varsha"}, write=True) == {"varsha": every}
    out = round_prefix(C.RUN_TAG, "varsha")
    assert bucket.written == [logo_step_key(out, sha) for sha in every]
    assert "retired logo step" in json.loads(bucket.objects[(S3_BUCKET, logo_step_key(out, A), None)])["reason"]
    clean = round_prefix(C.CLEAN_TAG, "varsha")
    assert (S3_BUCKET, logo_step_key(clean, A), None) not in bucket.objects             # a cleaned copy had no logo step
    assert (S3_BUCKET, logo_step_key(round_prefix(C.RUN_TAG, "sentas"), A), None) not in bucket.objects
    assert m.mark(bucket, {"varsha"}, write=True) == {"varsha": []}                       # idempotent


def test_main_prints_the_count_and_writes_nothing_on_a_dry_run(bucket, monkeypatch, capsys):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("CONFIG_DB_SNAPSHOTS", "prod-bucket/production/scraper/config/config.db@v1")
    monkeypatch.setattr(m.boto3, "client", lambda *a, **k: bucket)
    assert m.main() == 0
    assert "would mark (DRY_RUN) 4 results of ['varsha']" in capsys.readouterr().out
    assert bucket.written == []
