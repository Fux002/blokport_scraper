"""The one texture path, pinned (review wave C6). The produce queues the new-variant textures (plus the capped
upgrade drip), publishes that queue to a unique S3 key and dispatches ONE GPU texture job bound to it; the job
(prepare_variant_images.run) pulls the queue, drops the mints already on S3 (keeping upgrades), runs the generator,
uploads and marks the best-model set only on a clean run. These tests pin exactly that, for the path every deployed
environment takes (the lean :core has no torch), with every outside call faked: no FAL, no GPU, no S3, no Batch.
Only the two accessors below (where the produce's entry and the generator runner live) change when the texture
code moves; every assertion stays the same."""

from __future__ import annotations

import json
import subprocess
import types

import pytest

import stone_pipeline.prepare_variant_images as pvi
from deploy import texture_trigger
from stone_pipeline import catalog, refresh_images
from stone_pipeline.config import settings
from stone_pipeline.stages import image_prompts


def _queue_textures():
    """The produce's texture entry."""
    return catalog._auto_queue_images()


def _runner_owner():
    """The module that owns the generator runner (_generate_queued_images)."""
    return catalog


class _Settings:
    """The real settings, with auto_texture and images overridden (SETTINGS is frozen, so it is stood in for)."""

    def __init__(self, auto_texture, upgrade_batch):
        self.auto_texture = types.SimpleNamespace(enabled=auto_texture, queue="q", job_definition="jd")
        self.images = types.SimpleNamespace(upgrade_batch=upgrade_batch)

    def __getattr__(self, name):
        return getattr(settings.SETTINGS, name)


@pytest.fixture(autouse=True)
def _nothing_outside(monkeypatch):
    """No test here may reach a real subprocess, FAL or AWS: any such call fails the test."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"a real subprocess ran: {a}"))
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: pytest.fail("a real boto3 client was made"))
    monkeypatch.setattr(boto3, "Session", lambda *a, **k: pytest.fail("a real boto3 session was made"))


@pytest.fixture
def produce(tmp_path, monkeypatch):
    """The deployed produce's world: FAL_KEY set, no torch (the lean :core), a two-item queue, and fakes that
    record the queue publish and the GPU dispatch."""
    queue = tmp_path / "prompts_to_generate.json"
    queue.write_text(json.dumps([{"output_name": "slab_a"}, {"output_name": "slab_b"}]), encoding="utf-8")
    monkeypatch.setattr(image_prompts, "build", lambda: queue)
    monkeypatch.setenv("FAL_KEY", "present")
    import importlib.util
    real_find = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda m: None if m == "torch" else real_find(m))
    seen = {"published": [], "dispatched": [], "generated": []}

    def publish(path, key=None, client=None):
        seen["published"].append((path, key))
        return seen.get("publish_ok", True)

    def dispatch(queued, queue_key=None):
        seen["dispatched"].append((queued, queue_key))
        return seen.get("job_id", "job-1")
    monkeypatch.setattr(pvi, "publish_prompts", publish)
    monkeypatch.setattr(texture_trigger, "submit_texture_job", dispatch)
    monkeypatch.setattr(_runner_owner(), "_generate_queued_images", lambda: seen["generated"].append(1) or [])

    def configure(auto_texture=True, upgrade_batch=0):
        for module in (catalog, pvi):
            monkeypatch.setattr(module, "SETTINGS", _Settings(auto_texture, upgrade_batch))
    seen["configure"] = configure
    configure()
    return seen


def test_the_deployed_produce_queues_publishes_to_a_unique_key_and_dispatches_one_gpu_job(produce):
    assert _queue_textures() == 2
    assert produce["generated"] == []                                  # never generated in the produce
    [(path, key)] = produce["published"]
    assert path == pvi.PROMPTS_LOCAL and key.startswith(pvi.TEXTURE_QUEUE_PREFIX) and key.endswith(".json")
    assert produce["dispatched"] == [(2, key)]                        # one job, bound to exactly that queue


def test_each_produce_publishes_its_own_queue_key(produce):
    _queue_textures()
    _queue_textures()
    keys = [key for _, key in produce["published"]]
    assert len(set(keys)) == 2                                        # never one clobberable queue


def test_without_auto_texture_the_produce_only_queues(produce):
    produce["configure"](auto_texture=False)
    assert _queue_textures() == 2
    assert produce["published"] == [] and produce["dispatched"] == []


def test_a_queue_that_cannot_be_published_is_never_dispatched(produce, caplog):
    produce["publish_ok"] = False
    assert _queue_textures() == 2
    assert produce["dispatched"] == []
    assert "NOT dispatching" in caplog.text and "NO GPU job was dispatched" in caplog.text


def test_a_dispatch_that_fails_is_loud(produce, caplog):
    produce["job_id"] = None
    assert _queue_textures() == 2
    assert len(produce["dispatched"]) == 1 and "NO GPU job was dispatched" in caplog.text


def test_an_empty_queue_publishes_and_dispatches_nothing(produce, tmp_path, monkeypatch):
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(image_prompts, "build", lambda: empty)
    assert _queue_textures() == 0
    assert produce["published"] == [] and produce["dispatched"] == []


def test_without_fal_key_the_produce_still_dispatches_the_gpu_job(produce, monkeypatch):
    monkeypatch.delenv("FAL_KEY")
    assert _queue_textures() == 2
    assert len(produce["dispatched"]) == 1 and produce["generated"] == []


def test_the_upgrade_drip_reads_the_best_model_marker_and_its_cap(produce, tmp_path, monkeypatch):
    produce["configure"](upgrade_batch=5)
    monkeypatch.setattr(refresh_images, "load_refreshed", lambda client=None: {"slab_done"})
    asked = {}
    queue = tmp_path / "with_upgrades.json"
    queue.write_text(json.dumps([{"output_name": "slab_a"}, {"output_name": "slab_old", "mode": "upgrade"}]),
                     encoding="utf-8")
    monkeypatch.setattr(image_prompts, "build_with_upgrades",
                        lambda refreshed, cap: asked.update(refreshed=refreshed, cap=cap) or queue)
    assert _queue_textures() == 2
    assert asked == {"refreshed": {"slab_done"}, "cap": 5} and produce["dispatched"][0][0] == 2


# --- the GPU texture job (RUN_MODE=generate-textures -> prepare_variant_images.run) -----------------------
@pytest.fixture
def job(tmp_path, monkeypatch):
    """The GPU job's world: the produce's published queue (two mints, one already on S3, and one upgrade), the
    generator stack present, and fakes for the runner, the upload and the best-model marker."""
    items = [{"output_name": "slab_new"}, {"output_name": "slab_exists"},
             {"output_name": "slab_old", "mode": image_prompts.UPGRADE_MODE}]

    def fetch(client=None):
        pvi.PROMPTS_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        pvi.PROMPTS_LOCAL.write_text(json.dumps(items), encoding="utf-8")
        return True
    monkeypatch.setattr(pvi, "PROMPTS_LOCAL", tmp_path / "prompts_to_generate.json")
    monkeypatch.setattr(pvi, "_fetch_published_prompts", fetch)
    from stone_pipeline.stages import emit_catalog
    monkeypatch.setattr(emit_catalog, "_s3_variation_keys", lambda: {"slab_exists", "slab_old"})
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: [])
    seen = {"failed": [], "missing": [], "marked": []}
    monkeypatch.setattr(_runner_owner(), "_generate_queued_images", lambda: list(seen["failed"]))
    monkeypatch.setattr(pvi, "upload_variant_images",
                        lambda keys, client=None: (seen.update(uploaded=keys), (len(keys), list(seen["missing"])))[1])
    monkeypatch.setattr(refresh_images, "mark_best_model", lambda keys, client=None: seen["marked"].append(list(keys)))
    return seen


def test_the_gpu_job_generates_the_queue_minus_mints_already_on_s3_and_marks_a_clean_run(job):
    assert pvi.run() == 2
    assert job["uploaded"] == ["slab_new", "slab_old"]               # the existing mint dropped, the upgrade kept
    assert job["marked"] == [["slab_new", "slab_old"]]
    assert [i["output_name"] for i in json.loads(pvi.PROMPTS_LOCAL.read_text())] == ["slab_new", "slab_old"]


@pytest.mark.parametrize("failure", ["failed", "missing"])
def test_a_gpu_job_that_did_not_finish_cleanly_marks_nothing(job, failure):
    job[failure] = ["genetate_images.py"] if failure == "failed" else ["slab_new"]
    pvi.run()
    assert job["marked"] == []                                       # the Keys stay eligible for the next run
