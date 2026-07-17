"""The single generate -> de-bg -> upload-to-variations/ command (steps 2-4 of the variant-image flow).
The generation reuses the committed image_pipeline scripts (covered elsewhere); these tests lock the part
that was the actual gap: uploading each {Key}.png to the CORRECT <env>/variations/ prefix, the idempotent
queue, and the no-spend guard when the generator stack is absent."""

from __future__ import annotations

import json
import types

import stone_pipeline.prepare_variant_images as pvi


class _FakeS3:
    def __init__(self):
        self.calls = []

    def upload_file(self, local, bucket, key):
        self.calls.append((local, bucket, key))


def _fake_settings(dry_run: bool, bucket: str = "test-bucket"):
    # rebind the module's SETTINGS (frozen dataclass -> replace the name, don't mutate the field)
    return types.SimpleNamespace(s3=types.SimpleNamespace(dry_run=dry_run, bucket=bucket))


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


def test_run_guards_and_spends_nothing_when_generator_stack_absent(tmp_path, monkeypatch):
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": "slab_marble_a"}]), encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: ["fal_client", "torch"])
    # must not import/run the generator or touch S3 -- a fake that would explode if called
    monkeypatch.setattr(pvi, "upload_variant_images", lambda *a, **k: (_ for _ in ()).throw(AssertionError("uploaded on a blocked run")))
    assert pvi.run() == 0


def test_run_generates_then_uploads_when_ready(tmp_path, monkeypatch):
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": "slab_marble_a"}, {"output_name": "slab_marble_b"}]),
                 encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: [])
    import stone_pipeline.catalog as catalog
    monkeypatch.setattr(catalog, "_generate_queued_images", lambda: [])   # pretend both generated
    seen = {}
    monkeypatch.setattr(pvi, "upload_variant_images",
                        lambda keys, client=None: seen.update(keys=keys) or (len(keys), []))
    assert pvi.run() == 2
    assert seen["keys"] == ["slab_marble_a", "slab_marble_b"]
