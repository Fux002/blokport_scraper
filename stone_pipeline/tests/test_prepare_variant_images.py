"""The single generate -> de-bg -> upload-to-variations/ command (steps 2-4 of the variant-image flow).
The generation reuses the committed image_pipeline scripts (covered elsewhere); these tests lock the part
that was the actual gap: uploading each {Key}.png to the CORRECT <env>/variations/ prefix, the idempotent
queue, and the no-spend guard when the generator stack is absent."""

from __future__ import annotations

import dataclasses
import json
import types

import pytest

import stone_pipeline.prepare_variant_images as pvi
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.io import variety_icons

# icon pair ids (`{Key}/{category}`): a texture is one pair
PA = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666/slab"
PB = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666/block"
PC = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666/tile"
QA = "marble__carrara_00000000-1111-5222-8333-944455556666/slab"
QB = "marble__carrara_00000000-1111-5222-8333-944455556666/block"
QC = "marble__carrara_00000000-1111-5222-8333-944455556666/tile"
QN = "onyx__honey_11111111-1111-5222-8333-944455556666/slab"
QO = "onyx__honey_11111111-1111-5222-8333-944455556666/block"


def _pair(pid: str) -> tuple[str, str]:
    key, cat = variety_icons.pair_of(pid)
    return key, cat.name


def _png(folder, pid: str, data: bytes) -> None:
    """A pair's png under `folder`, nested as the generator writes it ({Key}/{category}.png)."""
    path = folder / f"{pid}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture(autouse=True)
def _no_dispatched_queue(monkeypatch):
    # default for run() tests: a dev box (no dispatched queue key), so _resolve_queue builds locally (which
    # those tests monkeypatch). Tests of the dispatched path set the key explicitly.
    monkeypatch.delenv(pvi.TEXTURE_QUEUE_KEY_ENV, raising=False)
    monkeypatch.delenv("AWS_BATCH_JOB_ID", raising=False)


class _FakeS3:
    def __init__(self):
        self.calls = []
        self.downloads = []

    def upload_file(self, local, bucket, key):
        self.calls.append((local, bucket, key))

    def download_file(self, bucket, key, local):
        self.downloads.append((bucket, key, local))


def _fake_settings(dry_run: bool, bucket: str = "test-bucket", auto_texture=SETTINGS.auto_texture):
    # rebind the module's SETTINGS (frozen dataclass -> replace the name, don't mutate the field). paths is
    # used by the _find_png rglob fallback; point workspace_root at a dir with no image_pipeline/ so a
    # missing key stays missing. auto_texture is the real one (the queue TTL, the script timeout) unless a test
    # sets its own.
    import pathlib
    return types.SimpleNamespace(
        s3=types.SimpleNamespace(dry_run=dry_run, bucket=bucket), auto_texture=auto_texture,
        paths=types.SimpleNamespace(workspace_root=pathlib.Path("/nonexistent-workspace-root")))


def test_upload_targets_the_variations_prefix_and_reports_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pvi, "DEBG_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    _png(tmp_path, PA, b"x")
    _png(tmp_path, PB, b"y")
    client = _FakeS3()
    uploaded, missing = pvi.upload_variant_images(
        [PA, PB, PC], client=client)
    # two present -> uploaded to variations/; one absent -> reported, not silently dropped
    assert uploaded == 2 and missing == [PC]
    keys = [k for _, _, k in client.calls]
    assert keys == [variety_icons.icon_key(*_pair(PA)), variety_icons.icon_key(*_pair(PB))]   # one icon per pair


def test_upload_is_noop_on_dry_run(tmp_path, monkeypatch):
    monkeypatch.setattr(pvi, "DEBG_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=True))
    _png(tmp_path, PA, b"x")
    client = _FakeS3()
    uploaded, missing = pvi.upload_variant_images([PA], client=client)
    assert uploaded == 0 and missing == [PA] and client.calls == []


def test_run_does_nothing_when_queue_is_empty(tmp_path, monkeypatch):
    empty = tmp_path / "prompts.json"
    empty.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: empty)
    assert pvi.run() == 0


def test_a_job_that_cannot_run_the_generator_fails_and_spends_nothing(tmp_path, monkeypatch):
    # a dispatched job without FAL_KEY or the stack must exit non-zero (the failed-job alert), never pass
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": PA}]), encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: ["torch", "FAL_KEY"])
    # must not import/run the generator or touch S3 -- fakes that would explode if called
    monkeypatch.setattr(pvi, "_generate_queued_images", lambda: pytest.fail("generated on a blocked run"))
    monkeypatch.setattr(pvi, "upload_variant_images", lambda *a, **k: pytest.fail("uploaded on a blocked run"))
    with pytest.raises(pvi.TextureJobFailed, match="FAL_KEY"):
        pvi.run()


def test_run_generates_then_uploads_when_ready(tmp_path, monkeypatch):
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": PA}, {"output_name": PB}]),
                 encoding="utf-8")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    monkeypatch.setattr(pvi, "_generator_blockers", lambda: [])
    monkeypatch.setattr(pvi, "_generate_queued_images", lambda: [])   # pretend both generated
    seen = {}
    monkeypatch.setattr(pvi, "upload_variant_images",
                        lambda keys, client=None: seen.update(keys=keys) or (len(keys), []))
    monkeypatch.setattr(pvi.refresh_images, "mark_best_model",
                        lambda keys, client=None: seen.update(marked=sorted(keys)) or True)
    assert pvi.run() == 2
    assert seen["keys"] == [PA, PB] and seen["marked"] == sorted([PA, PB])


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


def test_a_batch_job_without_its_queue_key_cannot_run(monkeypatch):
    # Batch names every job it runs (AWS_BATCH_JOB_ID): a texture job there was dispatched, and one with no queue key
    # cannot rebuild a queue (no local export), so it fails instead of finding nothing and exiting 0
    monkeypatch.setenv("AWS_BATCH_JOB_ID", "job-1")
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: pytest.fail("rebuilt on the GPU task"))
    with pytest.raises(pvi.TextureJobFailed, match=pvi.TEXTURE_QUEUE_KEY_ENV):
        pvi.run()
    assert pvi.main() == pvi.EXIT_CANNOT_RUN


def test_a_queue_that_cannot_be_written_locally_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.setattr(pvi, "PROMPTS_LOCAL", tmp_path / "q.json")
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(dry_run=False))
    client = _FakeS3()

    def disk_full(bucket, key, local):
        raise OSError(28, "No space left on device")
    client.download_file = disk_full
    assert pvi._fetch_published_prompts("dev/scraper/texture_queues/xyz.json", client=client) is False


@pytest.mark.parametrize("blob", [b"not json", b"\xff\xfe", b"{}", b'{QA: 1}', b"[1]", b"null"],
                         ids=["not-json", "not-utf8", "empty-object", "object", "not-objects", "null"])
def test_a_malformed_queue_cannot_run(tmp_path, monkeypatch, blob):
    q = tmp_path / "q.json"
    q.write_bytes(blob)
    monkeypatch.setattr(pvi, "_resolve_queue", lambda: q)
    with pytest.raises(pvi.TextureJobFailed, match="queue"):
        pvi.run()
    assert pvi.main() == pvi.EXIT_CANNOT_RUN                     # not a crash's 1, and never a quiet 0


def test_the_base_texture_error_has_an_exit_code(monkeypatch):
    monkeypatch.setattr(pvi, "run", lambda: (_ for _ in ()).throw(pvi.TextureJobError("unclassified")))
    assert pvi.main() == pvi.EXIT_CANNOT_RUN


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
    q.write_text(json.dumps([{"output_name": QA}, {"output_name": QB}]), encoding="utf-8")
    # QA already has its icon on S3 -> pruned from BOTH the queue file and the returned targets
    monkeypatch.setattr(pvi, "_existing_icons", lambda: {QA})
    kept = pvi._prune_already_on_s3(q, [QA, QB])
    assert kept == [QB]
    assert json.loads(q.read_text(encoding="utf-8")) == [{"output_name": QB}]


def test_prune_noop_when_s3_unreachable(tmp_path, monkeypatch):
    # S3 unreachable (None) -> generate all rather than wrongly skip a possibly-missing image
    q = tmp_path / "prompts.json"
    q.write_text(json.dumps([{"output_name": QA}]), encoding="utf-8")
    monkeypatch.setattr(pvi, "_existing_icons", lambda: None)
    assert pvi._prune_already_on_s3(q, [QA]) == [QA]
    assert json.loads(q.read_text(encoding="utf-8")) == [{"output_name": QA}]   # file untouched


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
    monkeypatch.setattr(pvi, "SETTINGS", _fake_settings(   # 1h TTL for the test
        dry_run=False, auto_texture=dataclasses.replace(SETTINGS.auto_texture, queue_ttl_seconds=3600)))
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


# -- what landed: only BEN2's output is a finished texture; an upload error is that key's miss ---------------
class _Job:
    """A GPU job's world on disk: the queue, image_pipeline/{images,to_upload}, a fake S3 and a recording
    marker. Nothing on S3 yet, the generator stack present."""

    def __init__(self, tmp_path, monkeypatch, keys, failing_upload=()):
        from botocore.exceptions import ClientError
        ip = tmp_path / "image_pipeline"
        (ip / "images").mkdir(parents=True)
        (ip / "to_upload").mkdir()
        q = tmp_path / "prompts.json"
        q.write_text(json.dumps([{"output_name": k} for k in keys]), encoding="utf-8")
        self.ip, self.uploads, self.marked, self.mark_ok = ip, [], [], True
        settings = _fake_settings(dry_run=False)
        settings.paths = types.SimpleNamespace(workspace_root=tmp_path)
        monkeypatch.setattr(pvi, "SETTINGS", settings)
        monkeypatch.setattr(pvi, "DEBG_OUTPUT_DIR", ip / "to_upload")
        monkeypatch.setattr(pvi, "GEN_OUTPUT_DIR", ip / "images")
        monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
        monkeypatch.setattr(pvi, "_generator_blockers", lambda: [])
        monkeypatch.setattr(pvi, "_existing_icons", lambda: set())
        job = self

        class _S3:
            def upload_file(self, local, bucket, key):
                if any(key.endswith(f"/{k}.png") for k in failing_upload):
                    raise ClientError({"Error": {"Code": "500", "Message": "S3 down"}}, "PutObject")
                job.uploads.append(key[len(variety_icons.VARIATIONS_PREFIX):])
        monkeypatch.setattr(pvi, "_s3_client", lambda: _S3())
        monkeypatch.setattr(pvi.refresh_images, "mark_best_model",
                            lambda keys, client=None: job.marked.append(sorted(keys)) or job.mark_ok)

    def leftovers(self, raw=(), debackgrounded=()):
        """FLUX wrote images/{Key}.png for `raw`; BEN2 wrote to_upload/{Key}.png for `debackgrounded`."""
        for k in raw:
            _png(self.ip / "images", k, b"with background")
        for k in debackgrounded:
            _png(self.ip / "to_upload", k, b"clean")

    def generated(self, raw=(), debackgrounded=(), failed=()):
        """The generator, which writes those outputs when the job runs it (_generate_queued_images)."""
        return lambda: self.leftovers(raw, debackgrounded) or list(failed)


def test_a_texture_ben2_did_not_finish_is_never_uploaded_or_marked(tmp_path, monkeypatch):
    job = _Job(tmp_path, monkeypatch, [QA, QB])
    monkeypatch.setattr(pvi, "_generate_queued_images",
                        job.generated(raw=[QA, QB], debackgrounded=[QA], failed=["rb_images.py"]))
    with pytest.raises(pvi.TexturesIncomplete, match="1 of 2"):
        pvi.run()
    assert job.uploads == [f"{QA}.png"]                  # slab_b's background copy in images/ never ships
    assert job.marked == [[QA]]                     # and is never marked final: the next produce re-mints it


def test_an_upload_error_is_that_keys_miss_and_the_rest_still_land(tmp_path, monkeypatch):
    job = _Job(tmp_path, monkeypatch, [QA, QB, QC], failing_upload=[QB])
    monkeypatch.setattr(pvi, "_generate_queued_images",
                        job.generated(debackgrounded=[QA, QB, QC]))
    with pytest.raises(pvi.TexturesIncomplete, match="1 of 3"):
        pvi.run()
    assert job.uploads == [f"{QA}.png", f"{QC}.png"]
    assert job.marked == [[QA, QC]]           # what landed is marked, so nothing is paid twice


def test_a_marker_that_was_not_written_is_a_partial_job(tmp_path, monkeypatch):
    job = _Job(tmp_path, monkeypatch, [QA])
    job.mark_ok = False
    monkeypatch.setattr(pvi, "_generate_queued_images", job.generated(debackgrounded=[QA]))
    with pytest.raises(pvi.TexturesIncomplete, match="marker NOT written"):
        pvi.run()
    assert job.uploads == [f"{QA}.png"]


def test_an_unreadable_marker_makes_the_produce_queue_mints_only(tmp_path, monkeypatch, caplog):
    q = tmp_path / "mints.json"
    q.write_text(json.dumps([{"output_name": QN}]), encoding="utf-8")
    monkeypatch.setattr(pvi, "SETTINGS", types.SimpleNamespace(
        images=types.SimpleNamespace(upgrade_batch=5), auto_texture=types.SimpleNamespace(enabled=False)))

    def unreadable(client=None):
        raise pvi.refresh_images.MarkerUnreadable("AccessDenied")
    monkeypatch.setattr(pvi.refresh_images, "load_refreshed", unreadable)
    monkeypatch.setattr(pvi.image_prompts, "build", lambda: q)
    monkeypatch.setattr(pvi.image_prompts, "build_with_upgrades",
                        lambda refreshed, cap: pytest.fail("upgrades queued against an unreadable marker"))
    assert pvi.queue_new_textures() == 1
    assert any(r.levelname == "ERROR" and "marker" in r.getMessage() for r in caplog.records)


def test_the_queue_build_fails_loud_on_a_bug_and_holds_on_an_unreadable_input(monkeypatch, caplog):
    # a broad except around the queue build swallowed a code bug as "queue build failed". Only an input
    # that cannot be read (a missing or malformed export, products or backbone file) is that; a bug fails the produce.
    monkeypatch.setattr(pvi, "SETTINGS", types.SimpleNamespace(
        images=types.SimpleNamespace(upgrade_batch=0), auto_texture=types.SimpleNamespace(enabled=False)))

    def bug():
        raise TypeError("a bug in the queue build")
    monkeypatch.setattr(pvi.image_prompts, "build", bug)
    with pytest.raises(TypeError):
        pvi.queue_new_textures()

    def unreadable():
        raise OSError("the products file cannot be read")
    monkeypatch.setattr(pvi.image_prompts, "build", unreadable)
    assert pvi.queue_new_textures() == 0
    assert any("queue build failed" in r.getMessage() for r in caplog.records)


def test_a_stale_local_texture_is_cleared_before_the_generator_runs(tmp_path, monkeypatch):
    # genetate_images resumes over an existing images/{Key}.png and BEN2 de-backgrounds it, so a PNG an
    # earlier run left on a dev box would be uploaded as this run's texture. The queued keys' outputs go first.
    job = _Job(tmp_path, monkeypatch, [QA])
    job.leftovers(raw=[QA, QO], debackgrounded=[QA])       # an earlier run's leftovers
    seen = {}

    def generate():
        seen["raw"] = (job.ip / "images" / f"{QA}.png").exists()
        seen["clean"] = (job.ip / "to_upload" / f"{QA}.png").exists()
        _png(job.ip / "to_upload", QA, b"this run")
        return []
    monkeypatch.setattr(pvi, "_generate_queued_images", generate)
    assert pvi.run() == 1
    assert seen == {"raw": False, "clean": False}
    assert (job.ip / "images" / f"{QO}.png").exists()                        # a key not queued is left alone
