"""The single generate -> de-bg -> upload-to-variations/ command (steps 2-4 of the variant-image flow).
The generation reuses the committed image_pipeline scripts (covered elsewhere); these tests lock the part
that was the actual gap: uploading each {Key}.png to the CORRECT <env>/variations/ prefix, the idempotent
queue, and the no-spend guard when the generator stack is absent."""

from __future__ import annotations

import json
import types

import pytest

import stone_pipeline.prepare_variant_images as pvi

@pytest.fixture(autouse=True)
def _no_dispatched_queue(monkeypatch):
    # default for run() tests: a dev box (no dispatched queue key), so _resolve_queue builds locally (which
    # those tests monkeypatch). Tests of the dispatched path set the key explicitly.
    monkeypatch.delenv(pvi.TEXTURE_QUEUE_KEY_ENV, raising=False)


class _FakeS3:
    def __init__(self):
        self.calls = []
        self.downloads = []

    def upload_file(self, local, bucket, key):
        self.calls.append((local, bucket, key))

    def download_file(self, bucket, key, local):
        self.downloads.append((bucket, key, local))


def _fake_settings(dry_run: bool, bucket: str = "test-bucket"):
    # rebind the module's SETTINGS (frozen dataclass -> replace the name, don't mutate the field). paths is
    # used by the _find_png rglob fallback; point workspace_root at a dir with no image_pipeline/ so a
    # missing key stays missing.
    import pathlib
    return types.SimpleNamespace(
        s3=types.SimpleNamespace(dry_run=dry_run, bucket=bucket),
        paths=types.SimpleNamespace(workspace_root=pathlib.Path("/nonexistent-workspace-root")))


def test_upload_targets_the_variations_prefix_and_reports_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pvi, "DEBG_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    (tmp_path / "slab_marble_a.png").write_bytes(b"x")
    (tmp_path / "slab_marble_b.png").write_bytes(b"y")
    client = _FakeS3()
    uploaded, missing = pvi.upload_variant_images(
        ["slab_marble_a", "slab_marble_b", "slab_marble_c"], client=client)
    # two present -> uploaded to variations/; one absent -> reported, not silently dropped
    assert uploaded == 2 and missing == ["slab_marble_c"]
    keys = [k for _, _, k in client.calls]
    assert keys == [f"{pvi.VARIATIONS_PREFIX}slab_marble_a.png",
                    f"{pvi.VARIATIONS_PREFIX}slab_marble_b.png"]
    assert all(k.endswith(".png") and "/variations/" in k for k in keys)   # correct prefix, one image per Key


def test_upload_is_noop_on_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pvi, "DEBG_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=True))
    (tmp_path / "slab_marble_a.png").write_bytes(b"x")
    client = _FakeS3()
    uploaded, missing = pvi.upload_variant_images(["slab_marble_a"], client=client)
    assert uploaded == 0 and missing == ["slab_marble_a"] and client.calls == []


def test_run_does_nothing_when_queue_is_empty(tmp_path, monkeypatch):
    empty = tmp_path / "prompts.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: empty)
    assert pvi.run() == 0


def test_a_job_that_cannot_run_the_generator_fails_and_spends_nothing(tmp_path, monkeypatch):
    # a dispatched job without FAL_KEY or the stack must exit non-zero (the failed-job alert), never pass
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": "slab_marble_a"}]), encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: ["torch", "FAL_KEY"])
    # must not import/run the generator or touch S3 -- fakes that would explode if called
    monkeypatch.setattr(pvi, "_generate_queued_images", lambda: pytest.fail("generated on a blocked run"))
    monkeypatch.setattr(pvi, "upload_variant_images", lambda *a, **k: pytest.fail("uploaded on a blocked run"))
    with pytest.raises(pvi.TextureJobFailed, match="FAL_KEY"):
        pvi.run()


def test_run_generates_then_uploads_when_ready(tmp_path, monkeypatch):
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": "slab_marble_a"}, {"output_name": "slab_marble_b"}]),
                 encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: [])
    monkeypatch.setattr(pvi, "_generate_queued_images", lambda: [])   # pretend both generated
    seen = {}
    monkeypatch.setattr(pvi, "upload_variant_images",
                        lambda keys, client=None: seen.update(keys=keys) or (len(keys), []))
    assert pvi.run() == 2
    assert seen["keys"] == ["slab_marble_a", "slab_marble_b"]


# -- queue passing: produce publishes the queue to S3; the bare GPU task pulls it -----------------------

def test_publish_prompts_noop_when_file_missing_or_dry_run(tmp_path, monkeypatch):
    key = "dev/scraper/texture_queues/abc123.json"
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    client = _FakeS3()
    assert pvi.publish_prompts(tmp_path / "absent.json", key, client=client) is False   # no file
    assert client.calls == []
    q = tmp_path / "p.json"; q.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=True))
    assert pvi.publish_prompts(q, key, client=_FakeS3()) is False                        # dry-run


def test_a_dispatched_job_uses_its_queue_and_never_rebuilds(monkeypatch):
    # the bare GPU task cannot rebuild the queue (no local export/products): it uses exactly its dispatched key
    monkeypatch.setenv(pvi.TEXTURE_QUEUE_KEY_ENV, "dev/scraper/texture_queues/xyz.json")
    asked = []
    monkeypatch.setattr(pvi, "_fetch_published_prompts", lambda key, client=None: asked.append(key) or True)
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: pytest.fail("rebuilt instead of using the dispatched queue"))
    assert pvi._resolve_queue() == pvi.PROMPTS_LOCAL
    assert asked == ["dev/scraper/texture_queues/xyz.json"]


def test_a_dispatched_job_whose_queue_cannot_be_read_fails(monkeypatch):
    # an unreadable dispatched queue is a failed job (alert), never a quiet local rebuild that finds nothing
    monkeypatch.setenv(pvi.TEXTURE_QUEUE_KEY_ENV, "dev/scraper/texture_queues/gone.json")
    monkeypatch.setattr(pvi, "_fetch_published_prompts", lambda key, client=None: False)
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: pytest.fail("rebuilt instead of failing"))
    with pytest.raises(pvi.TextureJobFailed, match="gone.json"):
        pvi.run()


def test_a_dev_box_builds_its_queue_locally_and_reads_nothing_from_s3(tmp_path, monkeypatch):
    q = tmp_path / "prompts.json"; q.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pvi, "_fetch_published_prompts", lambda key, client=None: pytest.fail("read S3 on a dev box"))
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    assert pvi._resolve_queue() == q


def test_publish_prompts_uses_the_per_dispatch_key(tmp_path, monkeypatch):
    # a unique per-dispatch key must be honoured (the anti-clobber contract), not the shared legacy key
    q = tmp_path / "prompts_to_generate.json"; q.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    client = _FakeS3()
    key = "dev/scraper/texture_queues/abc123.json"
    assert pvi.publish_prompts(q, key, client=client) is True
    assert client.calls == [(str(q), "test-bucket", key)]


def test_fetch_published_prompts_downloads_exactly_the_given_key(tmp_path, monkeypatch):
    monkeypatch.setattr(pvi, "PROMPTS_LOCAL", tmp_path / "q.json")
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    client = _FakeS3()
    assert pvi._fetch_published_prompts("dev/scraper/texture_queues/xyz.json", client=client) is True
    assert client.downloads == [("test-bucket", "dev/scraper/texture_queues/xyz.json", str(tmp_path / "q.json"))]


def test_prune_already_on_s3_drops_existing_and_rewrites_queue(tmp_path, monkeypatch):
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": "slab_a"}, {"output_name": "slab_b"}]), encoding="utf-8")
    # slab_a already has its image on S3 -> pruned from BOTH the queue file and the returned targets
    monkeypatch.setattr("stone_pipeline.stages.emit_catalog._s3_variation_keys", lambda: {"slab_a"})
    kept = pvi._prune_already_on_s3(q, ["slab_a", "slab_b"])
    assert kept == ["slab_b"]
    assert json.loads(q.read_text(encoding="utf-8")) == [{"output_name": "slab_b"}]


def test_prune_noop_when_s3_unreachable(tmp_path, monkeypatch):
    # S3 unreachable (None) -> generate all rather than wrongly skip a possibly-missing image
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": "slab_a"}]), encoding="utf-8")
    monkeypatch.setattr("stone_pipeline.stages.emit_catalog._s3_variation_keys", lambda: None)
    assert pvi._prune_already_on_s3(q, ["slab_a"]) == ["slab_a"]
    assert json.loads(q.read_text(encoding="utf-8")) == [{"output_name": "slab_a"}]   # file untouched


# -- stale texture-queue cleanup (litter self-heal) ----------------------------------------------------

class _FakeS3Lister:
    """S3 fake with a paginator + delete_objects, for the queue-cleanup test."""
    def __init__(self, contents):
        self._contents = contents          # list of {"Key","LastModified"}
        self.deleted = []

    def get_paginator(self, _name):
        contents = self._contents
        class _P:
            def paginate(self, **_kw):
                return [{"Contents": contents}]
        return _P()

    def delete_objects(self, Bucket, Delete):
        self.deleted.extend(o["Key"] for o in Delete["Objects"])


def test_prune_stale_queue_keys_deletes_only_old_objects(monkeypatch):
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    monkeypatch.setattr(pvi, "TEXTURE_QUEUE_TTL_SECONDS", 3600)   # 1h for the test
    now = datetime.now(timezone.utc)
    client = _FakeS3Lister([
        {"Key": "dev/scraper/texture_queues/old.json", "LastModified": now - timedelta(hours=5)},
        {"Key": "dev/scraper/texture_queues/fresh.json", "LastModified": now - timedelta(minutes=2)},
    ])
    assert pvi._prune_stale_queue_keys(client) == 1
    assert client.deleted == ["dev/scraper/texture_queues/old.json"]   # fresh (in-flight) never touched


def test_prune_stale_queue_keys_is_best_effort(monkeypatch):
    # a cleanup failure must NEVER raise (it can't be allowed to fail the produce)
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    class _Boom:
        def get_paginator(self, _n):
            raise RuntimeError("s3 down")
    assert pvi._prune_stale_queue_keys(_Boom()) == 0
