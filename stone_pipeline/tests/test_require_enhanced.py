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
