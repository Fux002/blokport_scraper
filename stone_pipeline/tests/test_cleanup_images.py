"""wipe_all_product_images: the 'fresh images' half of the pristine reset. Deletes EVERY hosted product
image + marker under <env>/products/, and NEVER the variant textures under <env>/variations/."""

from __future__ import annotations

from deploy import cleanup_images
from stone_pipeline.config.settings import ENV_SEGMENT


class _FakeS3:
    """Minimal in-memory S3: paginated list-by-prefix + delete_objects. Records deletes for assertions."""

    def __init__(self, keys, bodies=None):
        self.keys = set(keys)
        self.bodies = dict(bodies or {})       # key -> bytes, for the manifest get/put path
        self.deleted: list[str] = []

    def get_paginator(self, _op):
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(outer.keys) if k.startswith(Prefix)]}

        return _P()

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.keys.discard(o["Key"])
            self.bodies.pop(o["Key"], None)
            self.deleted.append(o["Key"])
        return {}

    def get_object(self, Bucket, Key):
        import io
        if Key not in self.bodies:
            raise Exception("NoSuchKey")
        return {"Body": io.BytesIO(self.bodies[Key])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.bodies[Key] = Body
        self.keys.add(Key)
        return {}


def _seed_keys(seg):
    return [
        f"{seg}/products/scraped/varsha/aaa.jpg",       # raw
        f"{seg}/products/improved/varsha/aaa.jpg",      # treated
        f"{seg}/products/enhanced/varsha/aaa.txt",      # marker
        f"{seg}/products/discarded/zucchi/bbb.json",    # marker
        f"{seg}/products/_manifest.json",               # manifest
        f"{seg}/variations/tile_travertine.png",        # TEXTURE -- must survive
        f"{seg}/variations/slab_carrara.png",           # TEXTURE -- must survive
    ]


def test_wipe_deletes_all_products_and_keeps_variations():
    seg = ENV_SEGMENT
    fake = _FakeS3(_seed_keys(seg))
    counts = cleanup_images.wipe_all_product_images(client=fake)
    # every product image + marker + manifest is gone
    assert not any(k.startswith(f"{seg}/products/") for k in fake.keys)
    # the variant textures are UNTOUCHED
    assert f"{seg}/variations/tile_travertine.png" in fake.keys
    assert f"{seg}/variations/slab_carrara.png" in fake.keys
    assert counts["scraped"] == counts["improved"] == counts["enhanced"] == counts["discarded"] == 1


def test_wipe_dry_run_reports_but_deletes_nothing():
    seg = ENV_SEGMENT
    fake = _FakeS3(_seed_keys(seg))
    counts = cleanup_images.wipe_all_product_images(client=fake, dry_run=True)
    assert counts["improved"] == 1 and fake.deleted == []          # counted, nothing removed
    assert f"{seg}/products/improved/varsha/aaa.jpg" in fake.keys   # still there


# -- wipe_source_product_images: the SCOPED (per-source) expensive restart -------------------------

import json as _json
import pytest


def _seed_multi(seg):
    keys = [
        f"{seg}/products/scraped/varsha/aaa.jpg",
        f"{seg}/products/improved/varsha/aaa.jpg",
        f"{seg}/products/enhanced/varsha/aaa.txt",
        f"{seg}/products/scraped/zucchi/ccc.jpg",       # OTHER source -- must survive
        f"{seg}/products/improved/zucchi/ccc.jpg",       # OTHER source -- must survive
        f"{seg}/products/_manifest.json",
        f"{seg}/variations/slab_carrara.png",            # TEXTURE -- must survive
    ]
    manifest = {
        "http://varsha/a": f"https://b/{seg}/products/improved/varsha/aaa.jpg",   # pruned
        "http://zucchi/c": f"https://b/{seg}/products/improved/zucchi/ccc.jpg",   # kept
    }
    bodies = {f"{seg}/products/_manifest.json": _json.dumps(manifest).encode()}
    return keys, bodies


def test_wipe_source_deletes_only_that_source_and_prunes_its_manifest_entries():
    seg = ENV_SEGMENT
    keys, bodies = _seed_multi(seg)
    fake = _FakeS3(keys, bodies)
    counts = cleanup_images.wipe_source_product_images("varsha", client=fake)
    # varsha product images gone
    assert not any(k.startswith(f"{seg}/products/") and "/varsha/" in k for k in fake.keys)
    # OTHER source + variant textures UNTOUCHED
    assert f"{seg}/products/scraped/zucchi/ccc.jpg" in fake.keys
    assert f"{seg}/products/improved/zucchi/ccc.jpg" in fake.keys
    assert f"{seg}/variations/slab_carrara.png" in fake.keys
    # the shared manifest is KEPT (not deleted), with only varsha's entry pruned
    assert f"{seg}/products/_manifest.json" in fake.keys
    man = _json.loads(fake.bodies[f"{seg}/products/_manifest.json"])
    assert "http://zucchi/c" in man and "http://varsha/a" not in man
    assert counts["scraped"] == counts["improved"] == counts["enhanced"] == 1
    assert counts["manifest_pruned"] == 1


def test_wipe_source_dry_run_deletes_nothing():
    seg = ENV_SEGMENT
    keys, bodies = _seed_multi(seg)
    fake = _FakeS3(keys, bodies)
    cleanup_images.wipe_source_product_images("varsha", client=fake, dry_run=True)
    assert not fake.deleted
    assert f"{seg}/products/improved/varsha/aaa.jpg" in fake.keys
    man = _json.loads(fake.bodies[f"{seg}/products/_manifest.json"])
    assert "http://varsha/a" in man            # manifest untouched on dry-run


def test_wipe_source_rejects_a_name_that_would_widen_the_prefix():
    with pytest.raises(ValueError):
        cleanup_images.wipe_source_product_images("../varsha", client=_FakeS3([]))


# -- raw-root layout (products/<source>/<sha>.jpg, processor-less s3 staging) is also wiped -----------

def test_wipe_source_also_deletes_raw_root_objects():
    seg = ENV_SEGMENT
    fake = _FakeS3([
        f"{seg}/products/varsha/rawroot.jpg",            # raw-root varsha -> deleted
        f"{seg}/products/improved/varsha/aaa.jpg",       # folder varsha -> deleted
        f"{seg}/products/zucchi/keep.jpg",               # raw-root OTHER source -> survives
        f"{seg}/variations/x.png",                       # texture -> survives
    ])
    counts = cleanup_images.wipe_source_product_images("varsha", client=fake)
    assert f"{seg}/products/varsha/rawroot.jpg" not in fake.keys
    assert f"{seg}/products/improved/varsha/aaa.jpg" not in fake.keys
    assert f"{seg}/products/zucchi/keep.jpg" in fake.keys
    assert f"{seg}/variations/x.png" in fake.keys
    assert counts["raw_root"] == 1


def test_wipe_all_also_deletes_raw_root_objects():
    seg = ENV_SEGMENT
    fake = _FakeS3([
        f"{seg}/products/varsha/rawroot.jpg",
        f"{seg}/products/improved/varsha/aaa.jpg",
        f"{seg}/variations/x.png",
    ])
    counts = cleanup_images.wipe_all_product_images(client=fake)
    assert not any(k.startswith(f"{seg}/products/") for k in fake.keys)   # every product image gone
    assert f"{seg}/variations/x.png" in fake.keys                        # texture kept
    assert counts["raw_root"] == 1
