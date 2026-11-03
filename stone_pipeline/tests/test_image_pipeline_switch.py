"""The per-source image pipeline switch (owner 2026-11-02; it replaces the retired enhancer's `enhance`, `fal_prompt` and
`watermarked`). On: a source's listings go through the photo process and the no_photo rule hides a product without a
standardised photo. Off: its products show the scraped photos as re-hosted (size-reduced, no GPU, no hosted call), stay
listed, and the photo process never picks them. A logo is a per-photo judge finding (leftover_mark) the clean step fixes,
never a source setting."""
from __future__ import annotations

import dataclasses
import inspect
import logging
import sqlite3
from contextlib import closing

import cv2
import numpy as np
import pytest

from deploy import standardise_batch as gpu
from deploy import standardise_run as proc
from stone_pipeline.config import server, store
from stone_pipeline.config.settings import ENV_NAME, ENV_SEGMENT, SETTINGS, STANDARDISE as C, ImageProcessingConfig, ImagesConfig
from stone_pipeline.config.sources import SourceConfig, load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import populate, writethrough
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.stages import emit, images
from stone_pipeline.standardise import photo

A, B = "a" * 64, "b" * 64
BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"
_RETIRED = ("enhance", "fal_prompt", "watermarked")


@pytest.fixture
def config_db(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    store.upsert_row({"source": "sentas", "source_code": "sen", "vendor": "Sentas Marble", "ports": ["Izmir"]})
    store.upsert_row({"source": "varsha", "source_code": "var", "vendor": "Varsha Stones", "ports": ["Brindisi"],
                      "image_pipeline": False})
    return tmp_path / "config.db"


# --- the setting, the admin API and the config.db migration ------------------------------------------------------------
def test_image_pipeline_defaults_on_and_round_trips_through_the_admin_api(config_db):
    assert load_source("sentas").image_pipeline is True and load_source("varsha").image_pipeline is False
    assert SourceConfig(source="new").image_pipeline is True
    code, body = server.dispatch("PUT", ["sources", "sentas"], {"image_pipeline": False})
    assert code == 200 and body["image_pipeline"] is False and load_source("sentas").image_pipeline is False
    code, body = server.dispatch("GET", ["sources", "sentas"], None)
    assert code == 200 and body["image_pipeline"] is False
    assert not set(_RETIRED) & set(body)


def test_the_retired_fields_an_older_admin_sends_are_ignored_with_a_warning(config_db, caplog):
    with caplog.at_level(logging.WARNING):
        code, body = server.dispatch("PUT", ["sources", "sentas"],
                                     {"enhance": False, "fal_prompt": "Remove the mark", "watermarked": True})
    assert code == 200 and body["image_pipeline"] is True and not set(_RETIRED) & set(body)
    assert any("enhance, fal_prompt, watermarked" in r.getMessage() for r in caplog.records)


def test_a_v8_store_turns_the_image_pipeline_on_for_every_source_and_drops_the_retired_columns(tmp_path):
    # owner 2026-11-02: the new pipeline is on by default for every existing source, whatever its old enhance said
    # (dev's enhance=0 only stopped the old enhancer's spend); turning it off is a deliberate admin choice
    db = tmp_path / "config.db"
    with closing(sqlite3.connect(db)) as raw:                                # the store as v8 left it
        raw.executescript(
            "CREATE TABLE source (source TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,"
            " source_code TEXT NOT NULL DEFAULT '', vendor TEXT NOT NULL DEFAULT '',"
            " origin_default TEXT NOT NULL DEFAULT '', ports TEXT,"
            " mode TEXT NOT NULL DEFAULT 'review', watermarked INTEGER NOT NULL DEFAULT 0,"
            " fal_prompt TEXT NOT NULL DEFAULT '', enhance INTEGER NOT NULL DEFAULT 1,"
            " emit_on_review INTEGER NOT NULL DEFAULT 1, default_bundle_size INTEGER NOT NULL DEFAULT 6,"
            " min_expected_rows INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL DEFAULT '');"
            "INSERT INTO source (source, source_code, watermarked, fal_prompt, enhance) VALUES"
            " ('varsha', 'var', 1, 'Remove the pink mark', 0), ('sentas', 'sen', 0, '', 1);"
            "PRAGMA user_version = 8;")
    sources = store.read_sources(db)
    assert (sources["varsha"].image_pipeline, sources["sentas"].image_pipeline) == (True, True)
    with closing(sqlite3.connect(db)) as raw:
        cols = {r[1] for r in raw.execute("PRAGMA table_info(source)")}
        assert "image_pipeline" in cols and not set(_RETIRED) & cols
        assert raw.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    store.upsert_source(dataclasses.replace(sources["varsha"], image_pipeline=False), path=db)
    assert store.read_sources(db)["varsha"].image_pipeline is False          # a later open keeps the admin's choice


def test_the_committed_sources_yaml_carries_no_retired_field():
    # the seed the store is built from: a retired key left in it would fail SourceConfig at load (fail loud)
    import yaml
    from stone_pipeline.config.sources import load_yaml_sources
    raw = yaml.safe_load(SETTINGS.paths.sources_yaml.read_text(encoding="utf-8"))
    assert raw and not [(src, key) for src, body in raw.items() for key in (body or {}) if key in _RETIRED]
    assert set(load_yaml_sources(SETTINGS.paths.sources_yaml)) == set(raw)


def test_a_retired_field_in_a_yaml_seed_fails_loud(tmp_path):
    from stone_pipeline.config.sources import load_yaml_sources
    seed = tmp_path / "sources.yaml"
    seed.write_text("varsha:\n  source_code: var\n  watermarked: true\n", encoding="utf-8")
    with pytest.raises(TypeError, match="watermarked"):
        load_yaml_sources(seed)


# --- off: the scraped photos, listed, never picked ---------------------------------------------------------------------
def _improved(sha, src):
    return f"{BASE}/improved/{src}/{sha}.png"


def _linking(monkeypatch, published):
    """link_standard over a passthrough run with `published` the main views on the pool, no owner decision."""
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: {
        "main": {sha: "e" for sha in published}, "white": {}, "studio": {}, "close": {}, "cutout": {}})
    monkeypatch.setattr(images, "_load_owner_removed", lambda cfg: set())
    monkeypatch.setattr(images, "_load_assigned", lambda cfg: {})
    return type("Cfg", (), {"mode": "passthrough", "public_base": f"{BASE}/"})()


def _hosted_row(src, key, sha):
    row = CanonicalRow(src_site=src, surrogate_key=key, format_value="Slab", image_keys=[_improved(sha, src)])
    images._slot_row(row, row.image_keys)                        # as the hosting step leaves it
    return row


def test_an_off_source_keeps_its_scraped_photos_while_an_on_source_gets_its_standardised_one(config_db, monkeypatch):
    cfg = _linking(monkeypatch, {A, B})
    on, off = _hosted_row("sentas", "1", A), _hosted_row("sentas", "2", B)
    images.link_standard([on], [on], cfg=cfg, image_pipeline=True)
    images.link_standard([off], [off], cfg=cfg, image_pipeline=False)
    assert on.standard_photo is True and on.thumbnail_key == f"{BASE}/standard/main/sentas/{A}.jpg"
    assert off.standard_photo is None and off.thumbnail_key == _improved(B, "sentas")
    assert off.standard_version is None and off.model_3d_key is None


def test_an_off_source_s_products_are_in_the_photo_list_and_its_conflict_set_is_made(config_db, monkeypatch):
    # the photo process stops loud on a sheet listing the photo list lacks and refuses a source without a stored conflict
    # set, so an off source's products are listed and its set is made like any source's
    cfg = _linking(monkeypatch, set())
    row = _hosted_row("varsha", "1", B)
    assert images.link_standard([row], [row], cfg=cfg, image_pipeline=False) == {"varsha": {}}
    assert row.own_photo_keys == [_improved(B, "varsha")]


def test_one_produce_uses_the_value_it_loaded_not_a_second_read_of_the_store(config_db, monkeypatch):
    # the store says varsha is off; the produce loaded it on (one read per run): the linking follows the produce
    cfg = _linking(monkeypatch, {B})
    row = _hosted_row("varsha", "1", B)
    images.link_standard([row], [row], cfg=cfg, image_pipeline=True)
    assert row.standard_photo is True


def _rule_on(monkeypatch):
    monkeypatch.setattr(populate, "SETTINGS", dataclasses.replace(
        SETTINGS, images=dataclasses.replace(SETTINGS.images, require_standard_photo=True)))


KEY = "slab_marble_carrara_0001"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A ledger with a synced, textured variety, so its products are servable (the write-through's real product write)."""
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    path = tmp_path / "dev.ledger"
    with Ledger.open(path, env=ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint()) as lg:
        now = now_iso()
        lg.upsert("variation", {
            "key": KEY, "branch": "slab", "type": "Marble", "name": "Carrara", "aliases": "[]",
            "image_url": f"https://bucket/variations/{KEY}.png", "volume": "", "medusa_id": "V1",
            "payload_hash": "", "state": "synced", "first_seen": now, "last_synced": now,
            "created_at": now, "updated_at": now}, pk=("key",))
    return path


def _product(standard_photo):
    return CanonicalRow(src_site="varsha", surrogate_key="X", variation_key=KEY, handle="h-x", title="Carrara",
                        inventory_quantity="3", thumbnail_key="products/X/thumb.webp",
                        product_image_keys=["products/X/1.webp"], standard_photo=standard_photo)


def _written(path, row, cfg):
    """(stored photo_ready, stock, stock reason) after the full produce's write of `row` (product, then its stock)."""
    assert writethrough.record_source([row], [], cfg, inventory_only=False, path=path)
    with Ledger.open(path, env=ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint()) as lg:
        sku = emit._sku(row, cfg)
        product, stock = lg.get("product", "sku", sku), lg.get("inventory", "sku", sku)
        return bool(product["photo_ready"]), stock["qty"], stock["reason"]


def test_the_no_photo_rule_never_hides_a_product_of_an_off_source(config_db, ledger, monkeypatch):
    _rule_on(monkeypatch)
    off = load_source("varsha")
    on = dataclasses.replace(off, image_pipeline=True)
    assert _written(ledger, _product(False), on) == (False, 0, "no_photo")         # on: hidden without a photo
    assert _written(ledger, _product(False), off) == (True, 3, None)               # off: shown on the next full produce
    assert _written(ledger, _product(None), off) == (True, 3, None)                # a run that cannot tell: still shown


def test_a_new_product_of_an_off_source_never_starts_hidden(config_db, ledger, monkeypatch):
    # with the pipeline on, a new product in a run that cannot tell starts hidden; an off source is never in that state
    _rule_on(monkeypatch)
    assert _written(ledger, _product(None), load_source("varsha")) == (True, 3, None)


def test_the_photo_process_never_picks_a_listing_of_an_off_source(config_db):
    listings = [{"key": "s1", "sku": "SEN-1", "source": "sentas", "format": "slab", "photos": [A]},
                {"key": "v1", "sku": "VAR-1", "source": "varsha", "format": "slab", "photos": [B]}]
    pick = dict(exclude=set(), slabs=set(), removed=set(), conflicts=set(), picks={})
    assert [l["key"] for l in proc.todo(listings, {}, **pick)] == ["s1"]
    # nor a chosen close-up of an off source (the whole-slab look-again, #486)
    records = {"s1": {"sha": A, "tag": "v2", "prefix": "p/"}, "v1": {"sha": B, "tag": "v2", "prefix": "p/"}}
    assert [l["key"] for l in proc.todo(listings, records, **pick)] == ["s1"]


def test_the_close_only_run_never_works_an_off_source(config_db, monkeypatch):
    import time
    from deploy import standardise_select as select
    records = [{"source": "sentas", "sha": A, "prefix": "p/", "tag": "v2", "key": "s1"},
               {"source": "varsha", "sha": B, "prefix": "p/", "tag": "v2", "key": "v1"}]
    monkeypatch.setattr(proc.close, "chosen", lambda s3, root: records)
    monkeypatch.setattr(proc.close, "listed", lambda recs, src: [r for r in recs if r["source"] == src])
    monkeypatch.setattr(proc.close, "undone", lambda s3, listed: listed)
    rounds = []
    monkeypatch.setattr(proc.gpu, "run_round", lambda batch, s3, tag, todo, run_id, deadline, spend, work:
                        rounds.append(todo) or [])
    proc.close_round(None, None, select.Spend(5.0), "R1", time.monotonic() + 3600, "root/", say=lambda m: None)
    assert rounds == [{"sentas": [A]}]


def _jpeg(w, h):
    rng = np.random.default_rng(0)
    return cv2.imencode(".jpg", rng.integers(0, 255, (h, w, 3), dtype=np.uint8))[1].tobytes()


def test_every_new_photo_is_re_hosted_size_reduced_and_kept_raw_with_no_hosted_call(config_db, tmp_path, monkeypatch):
    # varsha's photos carry a burned-in logo: they are hosted like every other source's (the photo process takes a logo
    # off per photo), never held for a de-watermark.
    store.upsert_row({**store.get_row("varsha"), "image_pipeline": True, "watermarked": True})   # an older admin's flag
    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s", public_base="https://cdn/x/",
                       processing=ImageProcessingConfig(enabled=True, keep_scraped=True))
    row = CanonicalRow(src_site="varsha", surrogate_key="w1", raw_image_urls=["http://x/logo.jpg"])
    stats = images.run([row], fetch=lambda url: _jpeg(3000, 1500), cfg=cfg)
    assert stats.staged == 1 and row.thumbnail_key and "/improved/varsha/" in row.thumbnail_key
    hosted = cv2.imread(str(next((tmp_path / "s").rglob("improved/varsha/*.jpg"))))
    assert max(hosted.shape[:2]) == cfg.processing.target_long_edge
    assert len(list((tmp_path / "s").rglob("scraped/varsha/*.jpg"))) == 1


# --- a logo is the photo's, found by the judge and cleaned; no source flag reaches a job --------------------------------
def test_a_logo_is_a_cleanable_judge_finding_not_a_step_of_the_standardiser():
    assert "logo" in C.JUDGE_PROMPT and "leftover_mark" in C.CLEAN_FIXES_KINDS
    assert not {"clean", "watermarked"} & set(inspect.signature(photo.standardise).parameters)


def test_no_gpu_job_carries_a_watermark_flag():
    class _Batch:
        def __init__(self):
            self.envs = []

        def submit_job(self, **job):
            self.envs.append({e["name"] for e in job["containerOverrides"]["environment"]})
            return {"jobId": str(len(self.envs))}

    class _S3:
        def put_object(self, **kw):
            pass

    batch = _Batch()
    gpu.submit(batch, _S3(), "q", "jd", "v9", {"varsha": [A]}, "R1", [])
    assert batch.envs and not any("WATERMARKED" in env for env in batch.envs)
