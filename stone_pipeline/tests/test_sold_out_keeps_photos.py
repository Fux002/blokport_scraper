"""A listing that leaves sale but keeps its record keeps its photos (owner 11-11, table 505; Mas: a factory reset spares a
gone listing's originals, and a vendor removal keeps its photos and textures). These are the listings gone at their
supplier (gone_at) and the delisted ones.

- The ledger names their photos: every (source, sha) their image links point at (a photo's views, its 3D slab, a tile
  listing's own rendered tile).
- The produce stores that kept set once per run (imagestore.kept_key) for the photo process. Publish leaves those photos
  exactly as published (views, 3D slab, assignment file, tile record), and a photo run refuses to start without the set
  (run a produce first).
- The factory wipe deletes every other product image and keeps every key of a kept photo.
Fixtures only."""

from __future__ import annotations

import dataclasses
import json

import pytest

from stone_pipeline import catalog, lifecycle
from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.io import image_wipe, imagestore
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import kept_photos
from stone_pipeline.photos import process as proc
from stone_pipeline.photos import publish as pub
from stone_pipeline.stages import images
from stone_pipeline.tests.test_standardise_process import NO_TILES, ROOT, _main, _published_bucket, _S3

A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64
URL = f"https://bucket.s3.eu-west-1.amazonaws.com/{ENV_SEGMENT}/products"


def _product(ledger, sku, *, qty=0, reason=None, gone_at=None, thumb=None, product_images=(), oriented=()):
    now = now_iso()
    ledger.upsert("variation", {"key": "marble__v_1", "type": "", "name": "x", "aliases": "[]", "medusa_id": "V",
                                "in_full": 1, "payload_hash": "", "state": "synced", "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))
    ledger.upsert("product", {"sku": sku, "source": "sen", "variation_key": "marble__v_1", "state": "synced",
                              "medusa_id": "P", "thumbnail_key": thumb,
                              "product_image_keys": json.dumps(list(product_images)),
                              "oriented_image_keys": json.dumps(list(oriented)), "created_at": now, "updated_at": now},
                  pk=("sku",))
    ledger.upsert("inventory", {"sku": sku, "qty": qty, "made_to_order": 0, "reason": reason, "gone_at": gone_at,
                                "updated_at": now}, pk=("sku",))


# --- which photo a link or a key is ------------------------------------------------------------------------------------
@pytest.mark.parametrize("link, photo", [
    (f"{URL}/improved/sentas/{A}.jpg", ("sentas", A)),
    (f"{URL}/standard/main/sentas/{A}.jpg", ("sentas", A)),
    (f"{ENV_SEGMENT}/products/standard/cutout/sentas/{A}.webp", ("sentas", A)),
    (f"{ENV_SEGMENT}/products/scraped/zucchi/{B}.png", ("zucchi", B)),
    (f"{ENV_SEGMENT}/products/sentas/{A}.jpg", ("sentas", A)),
    (f"{ENV_SEGMENT}/products/standard/conflicts/sentas.json", None),
    (f"{ENV_SEGMENT}/products/_manifest.json", None),
    ("", None),
])
def test_a_link_or_key_names_the_photo_it_holds(link, photo):
    assert imagestore.photo_of(link) == photo


# --- the kept set -------------------------------------------------------------------------------------------------------
def test_the_kept_photos_are_those_of_gone_and_delisted_listings(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _product(ledger, "SEN-GONE", reason="sold_out", gone_at="2026-11-01T00:00:00Z",
                 thumb=f"{URL}/standard/main/sentas/{A}.jpg", product_images=[f"{URL}/improved/sentas/{B}.jpg"])
        _product(ledger, "SEN-DEL", reason="delisted", oriented=[f"{URL}/improved/sentas/{C}.jpg"])
        _product(ledger, "SEN-LIVE", qty=4, thumb=f"{URL}/improved/sentas/{D}.jpg")
        assert kept_photos(ledger) == {"sentas": [A, B, C]}


def _bucket_mode(monkeypatch, mode, dry_run, stored):
    monkeypatch.setattr(images, "SETTINGS", dataclasses.replace(
        SETTINGS, s3=dataclasses.replace(SETTINGS.s3, dry_run=dry_run),
        images=dataclasses.replace(SETTINGS.images, mode=mode)))
    monkeypatch.setattr(images, "_stored_object", lambda key: stored.get(key))
    monkeypatch.setattr(images, "_put_object", lambda key, body: stored.__setitem__(key, body))


def test_the_produce_stores_the_kept_set_once_and_only_when_it_writes_the_bucket(monkeypatch):
    stored = {}
    _bucket_mode(monkeypatch, "s3", False, stored)
    images.save_kept({"sentas": [A]})
    assert json.loads(stored[imagestore.kept_key()]) == {"sentas": [A]}
    for mode, dry_run in (("s3", True), ("local", False)):
        stored.clear()
        _bucket_mode(monkeypatch, mode, dry_run, stored)
        images.save_kept({"sentas": [A]})
        assert stored == {}


def test_the_catalog_step_stores_the_ledger_s_kept_set(tmp_path, monkeypatch):
    path = tmp_path / "dev.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_WRITETHROUGH", "1")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(path))
    with Ledger.open(path, env="development") as ledger:
        _product(ledger, "SEN-GONE", reason="sold_out", gone_at="2026-11-01T00:00:00Z",
                 thumb=f"{URL}/improved/sentas/{A}.jpg")
    saved = []
    monkeypatch.setattr(images, "save_kept", saved.append)
    catalog.store_kept_photos()
    assert saved == [{"sentas": [A]}]


# --- the photo process --------------------------------------------------------------------------------------------------
def test_the_kept_set_reads_as_photos_and_a_missing_one_refuses():
    s3 = _S3({imagestore.kept_key(): json.dumps({"sentas": [A, B]}).encode()})
    assert pub.read_kept(s3) == {("sentas", A), ("sentas", B)}
    with pytest.raises(pub.NoKeptSet, match="run a produce first"):
        pub.read_kept(_S3({}))


def test_the_run_holds_the_unreadable_listings_photos_and_the_kept_ones():
    frozen = [{"source": "sentas", "photos": [A]}]
    assert proc.held_photos(frozen, {("zucchi", B)}) == {("sentas", A), ("zucchi", B)}


def test_publish_keeps_a_held_photo_s_tile_record():
    s3 = _published_bucket()
    record = imagestore.tile_record_key("sentas", B)
    s3._store(record, b"{}")
    s3._store(imagestore.standard_key("sentas", B, "main"), b"tile")
    pub.publish(s3, ROOT, NO_TILES, {}, say=lambda m: None, assigned={}, held={("sentas", B)})
    assert record in s3.objects and imagestore.standard_key("sentas", B, "main") in s3.objects


def test_the_photo_run_refuses_to_start_without_the_kept_set(monkeypatch, capsys):
    monkeypatch.setattr(proc, "process", lambda *a, **k: (_ for _ in ()).throw(pub.NoKeptSet(
        "no kept-photo set yet: run a produce first")))
    rc, relinks = _main(monkeypatch, keep_process=True)
    assert rc == 2 and relinks == [] and "no kept-photo set yet" in capsys.readouterr().out


# --- the factory wipe ---------------------------------------------------------------------------------------------------
class _Bucket:
    def __init__(self, keys):
        self.keys = set(keys)

    def get_paginator(self, _name):
        bucket = self

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(bucket.keys) if k.startswith(Prefix)]}
        return _P()

    def delete_objects(self, Bucket, Delete):
        self.keys -= {o["Key"] for o in Delete["Objects"]}


def test_the_factory_wipe_keeps_every_key_of_a_kept_photo():
    root = f"{ENV_SEGMENT}/products"
    kept = [f"{root}/scraped/sentas/{A}.jpg", f"{root}/improved/sentas/{A}.jpg", f"{root}/sentas/{A}.jpg",
            f"{root}/standard/main/sentas/{A}.jpg", f"{root}/standard/cutout/sentas/{A}.webp",
            f"{root}/standard/assigned/sentas/{A}.json", f"{root}/standard/tile/sentas/{A}.json"]
    gone = [f"{root}/scraped/sentas/{B}.jpg", f"{root}/improved/zucchi/{A}.jpg", f"{root}/standard/main/sentas/{B}.jpg",
            f"{root}/_manifest.json"]
    bucket = _Bucket(kept + gone)
    counts = image_wipe.wipe_all_product_images(bucket, keep={("sentas", A)})
    assert bucket.keys == set(kept) and counts["kept"] == len(kept)


def test_the_reset_wipes_with_the_ledger_s_kept_set(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(image_wipe, "wipe_all_product_images", lambda keep: seen.setdefault("keep", keep) or {})
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _product(ledger, "SEN-GONE", reason="sold_out", gone_at="2026-11-01T00:00:00Z",
                 thumb=f"{URL}/improved/sentas/{A}.jpg")
        lifecycle._wipe_product_images(ledger)
    assert seen["keep"] == {("sentas", A)}
