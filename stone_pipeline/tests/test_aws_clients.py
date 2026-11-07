"""Every AWS client the code builds, pinned (review wave C3): each construction site's (service, region, credentials
profile, the Config options it sets), recorded as it is built, with no network. Written against the code BEFORE the
one client factory (io/storage.aws_client) replaced the inline constructions, so the refactor provably changes no
site's effective client: the same retries, timeouts, connection pool, region and credentials as before.

The recorder stands in for boto3.client and boto3.Session; the client it hands back raises _Called on its first use, a
BaseException so the sites' own `except Exception` never swallows it: each site is driven only up to its first call."""
from __future__ import annotations

import dataclasses
import json
import sys
import types

import boto3
import pytest

from stone_pipeline.config import settings
from stone_pipeline.config.settings import S3_REGION

DEFAULTS = {}                                                                   # botocore's own (no Config)
PROCESS = {"retries": {"mode": "standard", "max_attempts": 10}}                 # the photo process's
POOLED = {**PROCESS, "max_pool_connections": 32}                               # the photo routes'
FAIL_FAST = {"connect_timeout": 3, "read_timeout": 10, "retries": {"max_attempts": 2}}       # the ledger snapshot's
BOUNDED = {"connect_timeout": 5, "read_timeout": 30, "retries": {"max_attempts": 3, "mode": "standard"}}
PROFILE = "ops"                                                                 # a named profile, set for the test


class _Called(BaseException):
    """A recorded client was used: the site is driven no further."""


class _Client:
    def __getattr__(self, name):
        raise _Called(name)


def _options(config):
    return {} if config is None else dict(config._user_provided_options)


@pytest.fixture
def built(monkeypatch):
    """[(service, region, profile, Config options)] of every client built, in order."""
    seen = []

    def client(service, region_name=None, config=None, **other):
        assert not other, other
        seen.append((service, region_name, None, _options(config)))
        return _Client()

    class Session:
        def __init__(self, profile_name=None, region_name=None, **other):
            assert not other, other
            self.profile, self.region = profile_name, region_name

        def client(self, service, config=None, **other):
            assert not other, other
            seen.append((service, self.region, self.profile, _options(config)))
            return _Client()
    monkeypatch.setattr(boto3, "client", client)
    monkeypatch.setattr(boto3, "Session", Session)
    return seen


def _drive(call):
    try:
        call()
    except _Called:
        pass


def _with_profile(monkeypatch, *modules):
    """The settings with a named credentials profile, as each module reads them (its own SETTINGS and settings')."""
    profiled = dataclasses.replace(settings.SETTINGS, s3=dataclasses.replace(settings.SETTINGS.s3,
                                                                             credentials_profile=PROFILE))
    for module in (settings, *modules):
        monkeypatch.setattr(module, "SETTINGS", profiled)
    return profiled


def _s3(options, profile=None):
    return ("s3", S3_REGION, profile, options)


# --- the default credential chain (no profile) ------------------------------------------------------------------------
def test_the_texture_trigger_s_batch_client(built, monkeypatch):
    from deploy import texture_trigger
    monkeypatch.setattr(texture_trigger, "SETTINGS", dataclasses.replace(texture_trigger.SETTINGS, auto_texture=dataclasses.replace(
        texture_trigger.SETTINGS.auto_texture, enabled=True, queue="q", job_definition="j")))
    _drive(lambda: texture_trigger.submit_texture_job(1, "k"))
    assert built == [("batch", S3_REGION, None, DEFAULTS)]


def test_fetch_inputs_clients(built, tmp_path):
    from deploy import fetch_inputs
    _drive(lambda: fetch_inputs.fetch_attributes(target=tmp_path / "attributes.csv"))
    _drive(fetch_inputs.main)
    assert built == [_s3(DEFAULTS), _s3(DEFAULTS)]


def test_cleanup_images_client(built):
    from deploy import cleanup_images
    _drive(cleanup_images.wipe_all_product_images)
    assert built == [_s3(DEFAULTS)]


@pytest.mark.parametrize("module, env", [
    ("standardise_source", {"SRC": "sentas"}), ("standardise_diag", {"SRC": "sentas", "ONLY_KEY": "k"}),
    ("standardise_close", {"CLEAN_TAG": "t", "SRC": "sentas"}), ("standardise_retouch", {"SRC": "sentas"}),
    ("mark_logo_step_results", {"DRY_RUN": "true"})])
def test_the_photo_job_entry_points_clients(built, monkeypatch, module, env):
    import importlib
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _drive(importlib.import_module(f"deploy.{module}").main)
    assert built == [_s3(DEFAULTS)]


def test_the_edit_test_job_s_client(built, monkeypatch):
    monkeypatch.setitem(sys.modules, "diffusers", types.SimpleNamespace(Flux2KleinPipeline=object))
    from deploy import standardise_edit_test
    _drive(standardise_edit_test.main)
    assert built == [_s3(DEFAULTS)]


def test_upload_artifacts_client(built):
    from deploy import upload_artifacts
    _drive(lambda: upload_artifacts.main("R1"))
    assert built == [_s3(DEFAULTS)]


def test_the_lifecycle_s_clients(built, monkeypatch, tmp_path):
    from stone_pipeline import lifecycle
    from stone_pipeline.reference import sync_variants_base
    paths = dataclasses.replace(settings.SETTINGS.paths, to_upload_dir=tmp_path, products_known_csv=tmp_path / "p.csv",
                                variants_export_csv=tmp_path / "v.csv")           # its prunes touch only these
    monkeypatch.setattr(settings, "SETTINGS", dataclasses.replace(settings.SETTINGS, paths=paths))
    monkeypatch.setattr(sync_variants_base, "write_deliverable", lambda base: None)
    _drive(lambda: lifecycle._publish_base_deliverable(tmp_path / "base.csv"))
    _drive(lifecycle._prune_stale_product_exports)
    _drive(lifecycle._prune_stale_medusa_export)
    assert built == [_s3(DEFAULTS)] * 3


def test_the_variety_base_publisher_s_client(built, tmp_path):
    from stone_pipeline.reference import sync_variants_base
    _drive(lambda: sync_variants_base.publish_base_to_s3(tmp_path / "variants_export_base.csv"))
    assert built == [_s3(DEFAULTS)]


def test_the_photo_process_s_clients(built, monkeypatch, tmp_path):
    from deploy import standardise_decisions, standardise_run
    monkeypatch.setenv("SCRAPER_GPU_QUEUE", "q")                    # what the GPU target reads before its first call
    monkeypatch.setenv("SCRAPER_STANDARDISE_JOBDEF", "j")
    _drive(lambda: standardise_run.reap("R1"))
    monkeypatch.setattr(standardise_run, "unconfigured", lambda close_only=False: [])
    monkeypatch.setattr(standardise_run, "stop_on_signal", lambda *spends: None)
    monkeypatch.delenv("SCRAPER_STANDARDISE_CLOSE_ONLY", raising=False)
    monkeypatch.delenv("SCRAPER_STANDARDISE_CLICKS_ONLY", raising=False)
    _drive(standardise_run.main)
    export = tmp_path / "decisions.json"
    export.write_text(json.dumps([]))
    for name, value in (("SCRAPER_ENV", "development"), ("DRY_RUN", "true"), ("DECISIONS_FILE", str(export))):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(standardise_decisions, "photo_listings", lambda s3: (_ for _ in ()).throw(_Called("sheet")))
    _drive(standardise_decisions.main)
    assert built == [_s3(PROCESS), ("batch", S3_REGION, None, PROCESS)] * 2 + [_s3(PROCESS)]


def test_the_photo_routes_client(built):
    from deploy import standardise_review
    standardise_review.client()
    assert built == [_s3(POOLED)]


def test_the_ledger_snapshot_s_client(built):
    from stone_pipeline.ledger import snapshot
    snapshot._s3()
    assert built == [_s3(FAIL_FAST)]


# --- the image and catalog clients: the named credentials profile -----------------------------------------------------
def test_the_variation_image_clients_honour_the_profile(built, monkeypatch):
    from stone_pipeline import catalog, prepare_variant_images, refresh_images
    from stone_pipeline.stages import emit_catalog
    _with_profile(monkeypatch, prepare_variant_images, refresh_images, emit_catalog)
    catalog._s3_image_copier()
    prepare_variant_images._s3_client()
    refresh_images._s3_client()
    _drive(emit_catalog._s3_variation_keys)
    assert built == [_s3(BOUNDED, PROFILE)] * 4


def test_the_image_stage_clients_honour_the_profile(built, monkeypatch):
    from stone_pipeline.stages import images, variety_color
    _with_profile(monkeypatch, images, variety_color)
    _drive(lambda: variety_color._fetch_product_image("https://b.s3.eu-west-1.amazonaws.com/dev/products/x.webp"))
    _drive(lambda: images._list_objects("dev/products/"))
    _drive(images._readonly_manifest)
    _drive(lambda: images._put_object("dev/x.json", b"{}"))
    _drive(lambda: images._get_object("dev/x.json"))
    assert built == [_s3(DEFAULTS, PROFILE)] * 5


@pytest.mark.parametrize("profile, expected", [(PROFILE, PROFILE), ("", None)])
def test_the_staging_backend_client_honours_its_profile(built, profile, expected):
    from stone_pipeline.io import storage
    storage.S3StorageBackend(bucket="b", region=S3_REGION, key_prefix="p", public_base="https://x/", profile=profile,
                             dry_run=False)._get_client()
    assert built == [_s3(DEFAULTS, expected)]


# --- every ledger open: bound to this deployment's backend (its fingerprint checked, a first bind stamped) or not ----
@pytest.fixture
def opened(monkeypatch, tmp_path):
    """(the ledger path the sites use, [(path, env, fingerprint)] of every Ledger.open)."""
    from stone_pipeline.ledger.db import Ledger
    path = tmp_path / "dev.db"
    path.touch()                                                               # the readers look for it first
    monkeypatch.setenv("SCRAPER_LEDGER_PATH", str(path))
    seen = []

    def open_(cls, where, env, backend_id_fingerprint=None):
        seen.append((str(where), env, backend_id_fingerprint))
        raise _Called("open")
    monkeypatch.setattr(Ledger, "open", classmethod(open_))
    return path, seen


def _bound(path):
    from stone_pipeline.ledger import writethrough
    return (str(path), writethrough.ENV_NAME, writethrough.backend_fingerprint())


def _unbound(path):
    from stone_pipeline.ledger import writethrough
    return (str(path), writethrough.ENV_NAME, None)


def test_the_ledger_writers_and_the_sync_side_open_it_bound(opened, monkeypatch, tmp_path):
    from stone_pipeline import lifecycle, produce
    from stone_pipeline.ledger import reset, server, sync, verify, writethrough
    from stone_pipeline.reference import loaders
    from stone_pipeline.stages import tree_build, validate
    path, seen = opened
    monkeypatch.setattr(loaders, "restricted_values", lambda: {"color": {"lilac"}})   # VE2 live: validate reads acked
    for call in (produce._frozen_waiting, produce._ledger_gate_state, produce._cold_start_stall,
                 lambda: lifecycle._ledger_op("probe", lambda ledger, s: {}), lifecycle.list_minted_variations,
                 lambda: sync.main([]), lambda: reset.main([]), lambda: verify.verify(path, tmp_path),
                 lambda: writethrough.open_ledger(), lambda: tree_build._acked_combination_tuples({}, {}),
                 lambda: validate._frozen_and_acked("sen")):
        _drive(call)
    handler = server.SyncHandler.__new__(server.SyncHandler)
    handler.headers, handler.path = {"Authorization": "Bearer t"}, "/sync/v1/status"
    handler.server = type("S", (), {"expected_token": "t", "ledger_path": path})()
    handler._respond = lambda code, payload: None
    _drive(lambda: handler._handle("GET"))
    assert seen == [_bound(path)] * 12


def test_a_bootstrap_the_seed_failed_still_opens_its_ledger_bound(opened, monkeypatch, tmp_path):
    from stone_pipeline.ledger import server, writethrough
    _, seen = opened
    monkeypatch.setattr(writethrough, "open_ledger", lambda where: (_ for _ in ()).throw(RuntimeError("no exports")))
    _drive(lambda: server.bootstrap_ledger_if_missing(tmp_path / "fresh.db"))
    assert seen == [_bound(tmp_path / "fresh.db.bootstrap.tmp")]


def test_the_readers_open_it_unbound(opened):
    from deploy import standardise_review
    from stone_pipeline.config import diagnostics, runner, scrape_status, server, varieties
    from stone_pipeline.ledger import simulate
    path, seen = opened
    for call in (standardise_review.hidden_products, diagnostics.ledger_image_counts, lambda: simulate.main([]),
                 runner._capture_counts, varieties._build_index, lambda: server._ledger_products("sen"),
                 scrape_status.ledger_product_counts):
        _drive(call)
    assert seen == [_unbound(path)] * 7
