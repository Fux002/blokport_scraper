"""wipe_all_product_images: the 'fresh images' half of the factory (pristine) reset. Deletes EVERY hosted
product image under <env>/products/ (the scraped originals, the re-hosted photos, the published standardised views,
anything else there such as the retired enhancer's markers, and the manifest), and NEVER the variant textures under
<env>/variations/ nor the photo process's stored results under <env>/standardise-test/, from which its next run
publishes the chosen photos again at no cost."""

from __future__ import annotations

from stone_pipeline.io import image_wipe as cleanup_images
from stone_pipeline.config.settings import ENV_SEGMENT


class _FakeS3:
    """Minimal in-memory S3: paginated list-by-prefix + delete_objects. Records deletes for assertions."""

    def __init__(self, keys):
        self.keys = set(keys)
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
            self.deleted.append(o["Key"])
        return {}


def _seed_keys(seg):
    return [
        f"{seg}/products/scraped/varsha/aaa.jpg",       # raw
        f"{seg}/products/improved/varsha/aaa.jpg",      # re-hosted
        f"{seg}/products/standard/main/varsha/aaa.jpg", # published standardised view
        f"{seg}/products/enhanced/varsha/aaa.txt",      # the retired enhancer's marker
        f"{seg}/products/discarded/zucchi/bbb.json",    # the retired enhancer's marker
        f"{seg}/products/_manifest.json",               # manifest
        f"{seg}/standardise-test/v2/varsha/aaa.main.jpg",   # the photo process's result -- must survive
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
    assert f"{seg}/standardise-test/v2/varsha/aaa.main.jpg" in fake.keys
    assert counts["scraped"] == counts["improved"] == counts["standard"] == 1 and counts["other"] == 2


def test_wipe_dry_run_reports_but_deletes_nothing():
    seg = ENV_SEGMENT
    fake = _FakeS3(_seed_keys(seg))
    counts = cleanup_images.wipe_all_product_images(client=fake, dry_run=True)
    assert counts["improved"] == 1 and fake.deleted == []          # counted, nothing removed
    assert f"{seg}/products/improved/varsha/aaa.jpg" in fake.keys   # still there


def test_wipe_all_also_deletes_raw_root_objects():
    # raw-root layout (products/<source>/<sha>.jpg, s3 staging without processing) is also wiped
    seg = ENV_SEGMENT
    fake = _FakeS3([
        f"{seg}/products/varsha/rawroot.jpg",
        f"{seg}/products/improved/varsha/aaa.jpg",
        f"{seg}/variations/x.png",
    ])
    counts = cleanup_images.wipe_all_product_images(client=fake)
    assert not any(k.startswith(f"{seg}/products/") for k in fake.keys)   # every product image gone
    assert f"{seg}/variations/x.png" in fake.keys                        # texture kept
    assert counts["other"] == 1


def test_scoped_wipe_no_longer_exists():
    # the per-source image wipe was removed: the factory reset is the ONLY routine that deletes product images
    assert not hasattr(cleanup_images, "wipe_source_product_images")
