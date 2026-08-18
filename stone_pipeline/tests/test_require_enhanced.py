"""The HARD publish gate: with require_enhanced on, a product image is published ONLY if the GPU actually
enhanced it (an enhanced/ marker exists). improved/ presence is not proof -- produce on the torch-free
:core writes a raw re-encode there. So a raw image can never reach the catalog; it is HELD until enhanced.

Covers both modes, on and off, and the fail-closed default (no markers -> hold, never publish raw).
"""

from __future__ import annotations

from hashlib import sha256

import pytest

from stone_pipeline.config.settings import ImagesConfig
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.stages import images


def _fake_fetch(mapping):
    return lambda url: mapping.get(url)


def _sha(b):
    return sha256(b).hexdigest()


@pytest.fixture
def gated_cfg(tmp_path):
    return ImagesConfig(mode="local", local_staging_dir=tmp_path / "s",
                        public_base="https://cdn/x/", require_enhanced=True)


@pytest.fixture
def ungated_cfg(tmp_path):
    return ImagesConfig(mode="local", local_staging_dir=tmp_path / "s",
                        public_base="https://cdn/x/", require_enhanced=False)


# --------------------------------------------------------------------------- #
#  s3/local mode
# --------------------------------------------------------------------------- #

def test_gate_publishes_only_enhanced(gated_cfg, monkeypatch):
    a, b = b"AAA", b"BBB"
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: {_sha(a)})   # only a enhanced
    row = CanonicalRow(src_site="x", surrogate_key="1", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/a.jpg": a, "http://x/b.jpg": b}), cfg=gated_cfg)
    assert len(row.product_image_keys) == 1 and _sha(a) in row.product_image_keys[0]   # a linked
    assert all(_sha(b) not in k for k in row.product_image_keys)                        # b (raw) HELD


def test_gate_holds_unenhanced_as_no_image(gated_cfg, monkeypatch):
    a = b"AAA"
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: set())          # NOTHING enhanced
    row = CanonicalRow(src_site="x", surrogate_key="2", is_block=False, raw_image_urls=["http://x/a.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/a.jpg": a}), cfg=gated_cfg)
    assert row.image_keys == []                                                        # raw -> HELD
    assert any(f.code == FlagCode.no_image for f in row.review_flags)                  # transient, retried
    assert not any(f.code == FlagCode.no_publishable_image for f in row.review_flags)  # not terminal


def test_gate_off_publishes_regardless(ungated_cfg, monkeypatch):
    a = b"AAA"
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    # _load_enhanced_set must NOT even be consulted when the gate is off
    monkeypatch.setattr(images, "_load_enhanced_set",
                        lambda cfg=None: (_ for _ in ()).throw(AssertionError("gate off must not load set")))
    row = CanonicalRow(src_site="x", surrogate_key="3", is_block=False, raw_image_urls=["http://x/a.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/a.jpg": a}), cfg=ungated_cfg)
    assert len(row.product_image_keys) == 1                                            # backward-compatible


# --------------------------------------------------------------------------- #
#  passthrough mode
# --------------------------------------------------------------------------- #

def test_passthrough_gate_holds_unmarked(monkeypatch):
    h_ok, h_raw = "a" * 64, "b" * 64
    monkeypatch.setattr(images, "_readonly_manifest", lambda: {
        "http://x/a.jpg": f"https://s3/dev/products/improved/x/{h_ok}.jpg",   # enhanced
        "http://x/b.jpg": f"https://s3/dev/products/improved/x/{h_raw}.jpg",  # raw re-encode in improved/
    })
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: {h_ok})         # only a marked
    cfg = ImagesConfig(mode="passthrough", require_enhanced=True)
    row = CanonicalRow(src_site="x", surrogate_key="4", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], cfg=cfg)
    assert row.product_image_keys == [f"https://s3/dev/products/improved/x/{h_ok}.jpg"]   # raw b HELD


# --------------------------------------------------------------------------- #
#  PAGE-DRIVEN publish marker: the per-source enhance/watermarked page settings decide who writes the
#  enhanced/ marker (the publish unlock). enhance=off + watermarked=off -> :core writes it (resize is the
#  whole job, no GPU); enhance=on / watermarked=on -> :core writes nothing, the GPU stamps it after its work.
# --------------------------------------------------------------------------- #

def _jpeg(h=120, w=180, fill=60):
    import cv2
    import numpy as np
    return cv2.imencode(".jpg", np.full((h, w, 3), fill, dtype=np.uint8))[1].tobytes()


def _local_marker_shas(staging_root):
    from pathlib import Path
    base = Path(staging_root) / "staging" / "enhanced"
    return {p.stem for p in base.rglob("*.txt")} if base.exists() else set()


def _proc_cfg():
    from stone_pipeline.config.settings import ImageProcessingConfig
    return ImageProcessingConfig(enabled=True, dewatermark=True, keep_scraped=True, write_preview=False)


@pytest.fixture
def core_no_gpu(monkeypatch):
    """Simulate the torch/FAL-free :core deterministically (independent of the test host): ESRGAN and FAL
    both unavailable, so enhance/de-watermark cannot run here -- exactly like the produce container."""
    import stone_pipeline.io.image_processing as ip
    monkeypatch.setattr(ip._ESRGANEnhancer, "available", lambda self: False)
    monkeypatch.setattr(ip._Dewatermarker, "available", lambda self: False)


def test_core_marks_and_publishes_resize_only_source_same_run(tmp_path, monkeypatch, core_no_gpu):
    """PAGE enhance=off + watermarked=off: the :core size-reduce IS the whole pipeline, so :core writes the
    enhanced/ marker itself and the product publishes in the SAME run under require_enhanced -- no GPU, no
    republish. This is the fix for 'enhance off should only resize'."""
    monkeypatch.setattr(images, "_enhance_sources", lambda: set())          # page: enhance OFF
    monkeypatch.setattr(images, "_watermarked_sources", lambda: set())      # page: watermarked OFF
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: _local_marker_shas(tmp_path / "s"))
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s", public_base="https://cdn/x/",
                       require_enhanced=True, processing=_proc_cfg())
    row = CanonicalRow(src_site="zucchi", surrogate_key="1", is_block=False, raw_image_urls=["http://x/a.jpg"])
    stats = images.run([row], fetch=_fake_fetch({"http://x/a.jpg": _jpeg()}), cfg=cfg)
    assert _local_marker_shas(tmp_path / "s"), "resize-only source must be marked complete on :core"
    assert stats.staged == 1 and len(row.product_image_keys) == 1          # publishes this run, no GPU


def test_core_does_not_mark_enhance_on_source(tmp_path, monkeypatch, core_no_gpu):
    """PAGE enhance=on: needs the GPU upscale that :core cannot do, so :core writes NO marker (size-reduce is
    not the whole pipeline) and the image is HELD for the GPU."""
    monkeypatch.setattr(images, "_enhance_sources", lambda: {"zucchi"})     # page: enhance ON
    monkeypatch.setattr(images, "_watermarked_sources", lambda: set())
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: _local_marker_shas(tmp_path / "s"))
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s", public_base="https://cdn/x/",
                       require_enhanced=True, processing=_proc_cfg())
    row = CanonicalRow(src_site="zucchi", surrogate_key="2", is_block=False, raw_image_urls=["http://x/a.jpg"])
    stats = images.run([row], fetch=_fake_fetch({"http://x/a.jpg": _jpeg()}), cfg=cfg)
    assert not _local_marker_shas(tmp_path / "s"), "enhance=on must NOT be marked complete on :core"
    assert stats.no_image == 1 and row.image_keys == []                    # held for the GPU
    assert any(f.code == FlagCode.no_image for f in row.review_flags)


def test_core_does_not_mark_watermarked_source(tmp_path, monkeypatch, core_no_gpu):
    """PAGE watermarked=on: needs FAL (unavailable on :core) -> dewatermark_failed -> held, no marker, no
    watermarked original in improved/."""
    monkeypatch.setattr(images, "_enhance_sources", lambda: set())          # enhance off
    monkeypatch.setattr(images, "_watermarked_sources", lambda: {"varsha"})  # page: watermarked ON
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: _local_marker_shas(tmp_path / "s"))
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s", public_base="https://cdn/x/",
                       require_enhanced=True, processing=_proc_cfg())
    row = CanonicalRow(src_site="varsha", surrogate_key="3", is_block=False, raw_image_urls=["http://x/a.jpg"])
    stats = images.run([row], fetch=_fake_fetch({"http://x/a.jpg": _jpeg()}), cfg=cfg)
    assert not _local_marker_shas(tmp_path / "s")
    assert stats.no_image == 1 and row.image_keys == []                    # held for the GPU de-watermark


def _spy_dewatermarker(monkeypatch):
    """Make the FAL de-watermarker AVAILABLE on :core and record every call; ESRGAN stays unavailable (no
    torch on :core). Returns the call list so a test can assert whether :core de-watermarked."""
    import stone_pipeline.io.image_processing as ip
    calls = []
    monkeypatch.setattr(ip._ESRGANEnhancer, "available", lambda self: False)
    monkeypatch.setattr(ip._Dewatermarker, "available", lambda self: True)

    def _fake_dw(self, pil, prompt=None):
        calls.append("dw")
        return ip.DewatermarkResult(pil, applied=True, failed=False)

    monkeypatch.setattr(ip._Dewatermarker, "process", _fake_dw)
    return calls


def test_core_dewatermarks_resize_only_watermarked_source(tmp_path, monkeypatch):
    """PAGE watermarked=on + enhance=off: de-watermark is a hosted FAL call (no GPU), so :core does it +
    resize + marks -> publishes THIS run, NO GPU trip. FAL runs on :core precisely because the source needs
    no ESRGAN."""
    calls = _spy_dewatermarker(monkeypatch)
    monkeypatch.setattr(images, "_enhance_sources", lambda: set())              # page: enhance OFF
    monkeypatch.setattr(images, "_watermarked_sources", lambda: {"varsha"})     # page: watermarked ON
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: _local_marker_shas(tmp_path / "s"))
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s", public_base="https://cdn/x/",
                       require_enhanced=True, processing=_proc_cfg())
    row = CanonicalRow(src_site="varsha", surrogate_key="1", is_block=False, raw_image_urls=["http://x/a.jpg"])
    stats = images.run([row], fetch=_fake_fetch({"http://x/a.jpg": _jpeg()}), cfg=cfg)
    assert calls == ["dw"]                                                      # :core de-watermarked (FAL)
    assert _local_marker_shas(tmp_path / "s")                                   # marked complete on :core
    assert stats.staged == 1 and len(row.product_image_keys) == 1              # publishes, no GPU


def test_core_defers_dewatermark_for_enhance_on_source(tmp_path, monkeypatch):
    """PAGE watermarked=on + enhance=on: the GPU de-watermarks + upscales in ONE pass, so :core must NOT
    de-watermark (FAL would be billed twice). :core holds it for the GPU; the de-watermarker is never called
    here, and no marker is written."""
    calls = _spy_dewatermarker(monkeypatch)
    monkeypatch.setattr(images, "_enhance_sources", lambda: {"varsha"})         # page: enhance ON
    monkeypatch.setattr(images, "_watermarked_sources", lambda: {"varsha"})     # page: watermarked ON
    monkeypatch.setattr(images, "_load_enhanced_set", lambda cfg=None: _local_marker_shas(tmp_path / "s"))
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    cfg = ImagesConfig(mode="local", local_staging_dir=tmp_path / "s", public_base="https://cdn/x/",
                       require_enhanced=True, processing=_proc_cfg())
    row = CanonicalRow(src_site="varsha", surrogate_key="2", is_block=False, raw_image_urls=["http://x/a.jpg"])
    stats = images.run([row], fetch=_fake_fetch({"http://x/a.jpg": _jpeg()}), cfg=cfg)
    assert calls == []                                                          # :core did NOT de-watermark
    assert not _local_marker_shas(tmp_path / "s")                              # not complete on :core
    assert stats.no_image == 1 and row.image_keys == []                        # held for the GPU


def test_passthrough_gate_off_links_all_improved(monkeypatch):
    h_ok, h_raw = "c" * 64, "d" * 64
    monkeypatch.setattr(images, "_readonly_manifest", lambda: {
        "http://x/a.jpg": f"https://s3/dev/products/improved/x/{h_ok}.jpg",
        "http://x/b.jpg": f"https://s3/dev/products/improved/x/{h_raw}.jpg"})
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    cfg = ImagesConfig(mode="passthrough", require_enhanced=False)
    row = CanonicalRow(src_site="x", surrogate_key="5", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], cfg=cfg)
    assert len(row.product_image_keys) == 2                                             # both linked (gate off)
