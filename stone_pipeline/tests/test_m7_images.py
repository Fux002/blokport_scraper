"""M7 DoD: re-running the stage re-uploads nothing; blocks slot to oriented
columns, slabs to product image columns; identical bytes dedup; placeholders are
blocked. Runs fully offline with an injected fetcher.
"""

from __future__ import annotations

import hashlib

import pytest

from stone_pipeline.config.settings import ImagesConfig
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.io import storage
from stone_pipeline.stages import images


@pytest.fixture
def local_cfg(tmp_path):
    return ImagesConfig(mode="local", local_staging_dir=tmp_path / "staging",
                        public_base="https://cdn.example/staging/")


def _fake_fetch(mapping):
    return lambda url: mapping.get(url)


def test_slab_slots_and_content_addressed(local_cfg):
    row = CanonicalRow(src_site="polonine", surrogate_key="1", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    fetch = _fake_fetch({"http://x/a.jpg": b"AAA", "http://x/b.jpg": b"BBB"})
    stats = images.run([row], fetch=fetch, cfg=local_cfg)
    assert stats.staged == 1
    assert len(row.product_image_keys) == 2
    assert row.oriented_image_keys == []
    assert row.thumbnail_key == row.product_image_keys[0]
    digest = hashlib.sha256(b"AAA").hexdigest()
    assert digest in row.product_image_keys[0]
    key = storage.content_key("polonine", digest)
    assert (local_cfg.local_staging_dir / "staging" / key).exists()


def test_block_slots_oriented(local_cfg):
    row = CanonicalRow(src_site="x", surrogate_key="2", is_block=True,
                       raw_image_urls=[f"http://x/{i}.jpg" for i in range(4)])
    fetch = _fake_fetch({f"http://x/{i}.jpg": bytes([i]) * 10 for i in range(4)})
    images.run([row], fetch=fetch, cfg=local_cfg)
    assert len(row.oriented_image_keys) == 4
    assert row.product_image_keys == []


def test_identical_bytes_dedup(local_cfg):
    row = CanonicalRow(src_site="x", surrogate_key="3", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    fetch = _fake_fetch({"http://x/a.jpg": b"SAME", "http://x/b.jpg": b"SAME"})
    images.run([row], fetch=fetch, cfg=local_cfg)
    assert len(row.product_image_keys) == 1


def test_rerun_uploads_nothing(local_cfg):
    row = CanonicalRow(src_site="x", surrogate_key="4", is_block=False,
                       raw_image_urls=["http://x/a.jpg"])
    fetch = _fake_fetch({"http://x/a.jpg": b"DATA"})
    first = images.run([row], fetch=fetch, cfg=local_cfg)
    assert first.bytes_uploaded > 0
    row2 = CanonicalRow(src_site="x", surrogate_key="4", is_block=False,
                        raw_image_urls=["http://x/a.jpg"])
    second = images.run([row2], fetch=fetch, cfg=local_cfg)
    assert second.bytes_uploaded == 0  # idempotent: nothing re-uploaded


def test_download_failure_flags_and_skips(local_cfg):
    row = CanonicalRow(src_site="x", surrogate_key="5", is_block=False,
                       raw_image_urls=["http://x/missing.jpg"])
    images.run([row], fetch=_fake_fetch({}), cfg=local_cfg)
    assert row.image_keys == []
    assert any(f.code == FlagCode.image_download_failed for f in row.review_flags)


def test_placeholder_blocked(local_cfg, monkeypatch):
    data = b"PLACEHOLDER"
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(images, "_load_placeholder_hashes", lambda: {digest})
    row = CanonicalRow(src_site="x", surrogate_key="6", is_block=False,
                       raw_image_urls=["http://x/ph.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/ph.jpg": data}), cfg=local_cfg)
    assert row.image_keys == []
    assert any(f.code == FlagCode.no_image for f in row.review_flags)


def test_passthrough_links_to_improved_s3(monkeypatch):
    # passthrough points product images at the IMPROVED S3 versions via the imageproc manifest,
    # never the raw source url; an image not in the manifest is DROPPED, not defaulted.
    monkeypatch.setattr(images, "_readonly_manifest",
                        lambda: {"http://x/a.jpg": "https://s3/dev/products/improved/x/aa.jpg"})
    cfg = ImagesConfig(mode="passthrough")
    row = CanonicalRow(src_site="x", surrogate_key="7", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], cfg=cfg)
    assert row.product_image_keys == ["https://s3/dev/products/improved/x/aa.jpg"]  # b.jpg dropped


def test_passthrough_falls_back_to_source_when_manifest_unreachable(monkeypatch):
    # fully-offline / no S3 -> empty manifest -> keep source urls so a local run still works
    monkeypatch.setattr(images, "_readonly_manifest", lambda: {})
    cfg = ImagesConfig(mode="passthrough")
    row = CanonicalRow(src_site="x", surrogate_key="7", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], cfg=cfg)
    assert row.product_image_keys == ["http://x/a.jpg", "http://x/b.jpg"]


def _jpeg_bytes(h=120, w=180, fill=50):
    import cv2
    import numpy as np
    ok, enc = cv2.imencode(".jpg", np.full((h, w, 3), fill, dtype=np.uint8))
    return enc.tobytes()


def test_processing_disabled_is_noop(local_cfg, monkeypatch):
    # default config has processing.enabled = False -> no ImageProcessor built.
    import stone_pipeline.io.image_processing as ip
    monkeypatch.setattr(ip, "ImageProcessor", lambda cfg: (_ for _ in ()).throw(
        AssertionError("processor must not be built when disabled")))
    row = CanonicalRow(src_site="varsha", surrogate_key="p1", is_block=False,
                       raw_image_urls=["http://x/a.jpg"])
    stats = images.run([row], fetch=_fake_fetch({"http://x/a.jpg": _jpeg_bytes()}), cfg=local_cfg)
    assert stats.processed == 0


def test_processing_enhances_and_is_idempotent(tmp_path):
    from stone_pipeline.config.settings import ImageProcessingConfig
    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s",
                       public_base="https://cdn/x/",
                       processing=ImageProcessingConfig(enabled=True, dewatermark=False,
                                                        write_preview=False))
    raw = _jpeg_bytes()
    row = CanonicalRow(src_site="varsha", surrogate_key="p2", is_block=False,
                       raw_image_urls=["http://x/a.jpg"])
    first = images.run([row], fetch=_fake_fetch({"http://x/a.jpg": raw}), cfg=cfg)
    assert first.processed == 1 and first.bytes_uploaded > 0 and row.product_image_keys
    # re-run: source already hosted -> no reprocess, no re-upload
    row2 = CanonicalRow(src_site="varsha", surrogate_key="p2", is_block=False,
                        raw_image_urls=["http://x/a.jpg"])
    second = images.run([row2], fetch=_fake_fetch({"http://x/a.jpg": raw}), cfg=cfg)
    assert second.processed == 0 and second.bytes_uploaded == 0


def test_rescrape_same_url_different_bytes_not_reprocessed(tmp_path):
    """A repeated scrape where the supplier RE-ENCODES the same photo (same URL,
    different bytes) must NOT create a duplicate or re-process — the url->key
    manifest keys idempotency on the source URL, not the raw bytes."""
    import cv2
    import numpy as np
    arr = np.full((120, 180, 3), 50, dtype=np.uint8)
    a = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    b = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
    assert a != b  # genuinely different bytes for the same photo

    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s", public_base="https://cdn/x/")
    url = "http://x/a.jpg"

    def run(payload):
        row = CanonicalRow(src_site="varsha", surrogate_key="r", is_block=False, raw_image_urls=[url])
        return images.run([row], fetch=_fake_fetch({url: payload}), cfg=cfg), row.product_image_keys[0]

    s1, key1 = run(a)
    s2, key2 = run(b)  # re-encoded on the "next scrape"
    assert s1.bytes_uploaded > 0
    assert s2.bytes_uploaded == 0          # not re-uploaded
    assert key1 == key2                    # same stored object reused
    stored = list((tmp_path / "s").rglob("*.jpg"))
    assert len(stored) == 1                # exactly one object, no duplicate
