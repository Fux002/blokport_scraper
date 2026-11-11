"""The one texture path, pinned (review wave C6). The produce queues the new-variant textures (plus the capped
upgrade drip), publishes that queue to a unique S3 key and dispatches ONE GPU texture job bound to it; the job
(prepare_variant_images.run) pulls the queue, drops the mints already on S3 (keeping upgrades), runs the generator,
uploads and marks the best-model set only on a clean run. These tests pin exactly that, with every outside call
faked: no FAL, no GPU, no S3, no Batch. The texture code lives in prepare_variant_images, and nothing it imports
reaches back into catalog (the import-graph test below)."""

from __future__ import annotations

import ast
import json
import subprocess
import types

import pytest

import stone_pipeline.prepare_variant_images as pvi
from stone_pipeline.io import texture_job as texture_trigger
from stone_pipeline import refresh_images
from stone_pipeline.config import settings
from stone_pipeline.stages import image_prompts


def _queue_textures():
    """The produce's texture entry (catalog.run calls it)."""
    return pvi.queue_new_textures()


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
    monkeypatch.setattr(boto3, "resource", lambda *a, **k: pytest.fail("a real boto3 resource was made"))


@pytest.fixture(params=["lean", "generator_stack"])
def produce(request, tmp_path, monkeypatch):
    """The produce's world, on the lean :core (no torch, every deployed env) and on a box with the whole
    generator stack (the same path: the produce never generates): FAL_KEY set, a two-item queue, and fakes that
    record the queue publish and the GPU dispatch."""
    queue = tmp_path / "prompts_to_generate.json"
    queue.write_text(json.dumps([{"output_name": "slab_a"}, {"output_name": "slab_b"}]), encoding="utf-8")
    monkeypatch.setattr(image_prompts, "build", lambda: queue)
    monkeypatch.setenv("FAL_KEY", "present")
    import importlib.util
    real_find = importlib.util.find_spec
    lean = request.param == "lean"
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda m: (None if lean else object()) if m in pvi.GEN_DEPS else real_find(m))
    seen = {"published": [], "dispatched": [], "generated": [], "queue": queue}

    def publish(path, key, client=None):
        seen["published"].append((path, key))
        return seen.get("publish_ok", True)

    def dispatch(queued, queue_key):
        seen["dispatched"].append((queued, queue_key))
        return seen.get("job_id", "job-1")
    monkeypatch.setattr(pvi, "publish_prompts", publish)
    monkeypatch.setattr(texture_trigger, "submit_texture_job", dispatch)
    monkeypatch.setattr(pvi, "_generate_queued_images", lambda: seen["generated"].append(1) or [])

    def configure(auto_texture=True, upgrade_batch=0):
        monkeypatch.setattr(pvi, "SETTINGS", _Settings(auto_texture, upgrade_batch))
    seen["configure"] = configure
    configure()
    return seen


def test_the_deployed_produce_queues_publishes_to_a_unique_key_and_dispatches_one_gpu_job(produce):
    assert _queue_textures() == 2
    assert produce["generated"] == []                                  # never generated in the produce
    [(path, key)] = produce["published"]
    assert path == produce["queue"]                                   # exactly the file the build wrote
    assert key.startswith(pvi.TEXTURE_QUEUE_PREFIX) and key.endswith(".json")
    assert produce["dispatched"] == [(2, key)]                        # one job, bound to exactly that queue


def test_each_produce_publishes_its_own_queue_key(produce):
    _queue_textures()
    _queue_textures()
    keys = [key for _, key in produce["published"]]
    assert len(set(keys)) == 2                                        # never one clobberable queue


def test_without_auto_texture_the_produce_only_queues_and_says_nothing_generates(produce, caplog):
    produce["configure"](auto_texture=False)
    assert _queue_textures() == 2
    assert produce["published"] == [] and produce["dispatched"] == [] and produce["generated"] == []
    [warning] = [r for r in caplog.records if "AUTO_TEXTURE is off" in r.getMessage()]
    assert warning.levelname == "WARNING" and "stay HELD" in warning.getMessage()


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


# icon pair ids (`{Key}/{category}`): a texture is one pair (spec rev 8 section 4); NEW and OLD sort in queue order
_K = "marble__carrara_00000000-1111-5222-8333-944455556666"
NEW, EXISTS, OLD, UP = f"{_K}/block", f"{_K}/slab", f"{_K}/tile", "onyx__honey_11111111-1111-5222-8333-944455556666/slab"


def _pair(pid: str) -> tuple[str, str]:
    key, _, name = pid.partition("/")
    return key, name


# --- the GPU texture job (RUN_MODE=generate-textures -> prepare_variant_images.run) -----------------------
@pytest.fixture
def job(tmp_path, monkeypatch):
    """The GPU job's world: the produce's published queue (two mints, one already on S3, and one upgrade), the
    generator stack present, and fakes for the runner, the upload and the best-model marker."""
    items = [{"output_name": NEW}, {"output_name": EXISTS},
             {"output_name": OLD, "mode": image_prompts.UPGRADE_MODE}]

    def fetch(key, client=None):
        assert key == "dev/scraper/texture_queues/job.json"           # exactly the dispatched queue
        pvi.PROMPTS_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        pvi.PROMPTS_LOCAL.write_text(json.dumps(items), encoding="utf-8")
        return True
    monkeypatch.setattr(pvi, "PROMPTS_LOCAL", tmp_path / "prompts_to_generate.json")
    monkeypatch.setattr(pvi, "_fetch_published_prompts", fetch)
    monkeypatch.setenv(pvi.TEXTURE_QUEUE_KEY_ENV, "dev/scraper/texture_queues/job.json")
    monkeypatch.setattr(pvi, "_existing_icons", lambda: {EXISTS, OLD})
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: [])
    seen = {"failed": [], "missing": [], "marked": []}
    monkeypatch.setattr(pvi, "_generate_queued_images", lambda: list(seen["failed"]))
    monkeypatch.setattr(pvi, "upload_variant_images",
                        lambda keys, client=None: (seen.update(uploaded=keys), (len(keys), list(seen["missing"])))[1])
    monkeypatch.setattr(refresh_images, "mark_best_model",
                        lambda keys, client=None: seen["marked"].append(list(keys)) or True)
    return seen


def test_the_gpu_job_generates_the_queue_minus_mints_already_on_s3_and_marks_a_clean_run(job):
    assert pvi.run() == 2
    assert job["uploaded"] == [NEW, OLD]                             # the existing mint dropped, the upgrade kept
    assert job["marked"] == [[NEW, OLD]]
    assert [i["output_name"] for i in json.loads(pvi.PROMPTS_LOCAL.read_text())] == [NEW, OLD]


def test_a_partial_gpu_job_marks_only_what_landed_and_exits_partial(job, monkeypatch):
    # loud (the failed-job alert) without a re-charge: what landed is marked best-model, so neither a retry nor
    # the upgrade drip pays for it again; the failed Key stays unmarked for the next produce's queue
    job["missing"] = [NEW]
    with pytest.raises(pvi.TexturesIncomplete, match="1 of 2"):
        pvi.run()
    assert job["marked"] == [[OLD]]
    monkeypatch.setattr(pvi, "run", lambda: (_ for _ in ()).throw(pvi.TexturesIncomplete("1 of 2")))
    assert pvi.main() == pvi.EXIT_PARTIAL


def test_the_job_exit_codes_are_distinct(monkeypatch):
    monkeypatch.setattr(pvi, "run", lambda: 2)
    assert pvi.main() == 0
    monkeypatch.setattr(pvi, "run", lambda: (_ for _ in ()).throw(pvi.TextureJobFailed("missing ['FAL_KEY']")))
    assert pvi.main() == pvi.EXIT_CANNOT_RUN
    assert len({0, 1, 2, pvi.EXIT_CANNOT_RUN, pvi.EXIT_PARTIAL}) == 5   # never a crash's 1 or the shell's 2


def test_what_a_partial_job_left_unmarked_is_queued_again_by_the_next_produce(monkeypatch):
    # the failed upgrade (unmarked) leads the next drip; the failed mint (still imageless) is a mint again
    marked = {OLD}                                                   # the partial job marked what landed
    monkeypatch.setattr(image_prompts, "product_backed_pairs", lambda: {_pair(NEW), _pair(OLD), _pair(UP)})
    icons = {OLD, UP}                                                # NEW never landed: no icon on S3
    assert image_prompts.upgrade_candidates(marked, cap=1, icons=icons) == [UP]
    assert NEW not in image_prompts.upgrade_candidates(marked, cap=10, icons=icons)   # no icon: build() mints it


# --- one way: the texture code never imports the produce back ----------------------------------------------
def _imports(module) -> set[str]:
    """Every stone_pipeline module a module imports, at top level or inside a function."""
    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names |= {node.module} | {f"{node.module}.{a.name}" for a in node.names}
    return {n for n in names if n.startswith("stone_pipeline")}


def test_the_texture_modules_never_import_the_produce_back():
    from stone_pipeline import catalog
    assert "stone_pipeline.prepare_variant_images" in _imports(catalog)        # the one direction
    assert not any(n.startswith("stone_pipeline.catalog") for n in _imports(pvi))
    assert not any(n.startswith(("stone_pipeline.catalog", "stone_pipeline.prepare_variant_images"))
                   for n in _imports(refresh_images))
