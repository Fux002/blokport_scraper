"""Bad-image discard, PRODUCER side (the image pipeline).

Stage 7 reads the discard pool (content sha256 of images the CLIP classifier rejected as non-stone) and,
when a variant's EVERY scraped image is discarded, emits the terminal no_publishable_image flag -- a
known-good empty that publishes imageless, NOT a transient no_image. Partial discards publish the
survivors. A discard mixed with a mere download failure stays transient (retry), never terminal.

The classifier itself fails safe: with no torch/weights it discards nothing (keep=True), so the pipeline
can never blank a catalogue by accident. The margin/reason math is covered where torch is available.
"""

from __future__ import annotations

from hashlib import sha256

import pytest

from stone_pipeline.config.settings import ImageProcessingConfig, ImagesConfig
from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.io import imagestore
from stone_pipeline.io.image_processing import ImageProcessor
from stone_pipeline.stages import images


@pytest.fixture
def local_cfg(tmp_path):
    return ImagesConfig(mode="local", local_staging_dir=tmp_path / "staging",
                        public_base="https://cdn.example/staging/")


def _fake_fetch(mapping):
    return lambda url: mapping.get(url)


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
#  imagestore layout: content identity is recoverable from any url/key
# --------------------------------------------------------------------------- #

def test_sha_from_url_and_discarded_key():
    h = "a" * 64
    assert imagestore.sha_from_url(f"https://s3/dev/products/improved/x/{h}.jpg") == h
    assert imagestore.sha_from_url(f"{imagestore._PRODUCTS}/discarded/x/{h}.json") == h
    assert imagestore.sha_from_url("https://s3/dev/products/improved/x/logo.png") is None
    assert imagestore.sha_from_url("") is None
    assert imagestore.discarded_key("varsha", h) == f"{imagestore._PRODUCTS}/discarded/varsha/{h}.json"


# --------------------------------------------------------------------------- #
#  Stage 7 emit rule (s3/local): terminal vs transient vs partial
# --------------------------------------------------------------------------- #

def test_all_images_discarded_is_terminal(local_cfg, monkeypatch):
    a, b = b"AAA", b"BBB"
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: {_sha(a), _sha(b)})
    row = CanonicalRow(src_site="x", surrogate_key="1", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/a.jpg": a, "http://x/b.jpg": b}), cfg=local_cfg)
    assert row.image_keys == []
    assert any(f.code == FlagCode.no_publishable_image for f in row.review_flags)   # terminal
    assert not any(f.code == FlagCode.no_image for f in row.review_flags)           # exactly one flag


def test_partial_discard_publishes_survivors(local_cfg, monkeypatch):
    a, b = b"AAA", b"BBB"
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: {_sha(a)})   # only a is non-stone
    row = CanonicalRow(src_site="x", surrogate_key="2", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/a.jpg": a, "http://x/b.jpg": b}), cfg=local_cfg)
    assert len(row.product_image_keys) == 1 and _sha(b) in row.product_image_keys[0]
    assert not any(f.code in (FlagCode.no_publishable_image, FlagCode.no_image) for f in row.review_flags)


def test_discard_plus_download_failure_stays_transient(local_cfg, monkeypatch):
    a = b"AAA"
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: {_sha(a)})
    row = CanonicalRow(src_site="x", surrogate_key="3", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/missing.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/a.jpg": a}), cfg=local_cfg)   # missing fails to fetch
    assert row.image_keys == []
    # one image merely failed to download -> we cannot say the variant is all-non-stone -> retry, not terminal
    assert any(f.code == FlagCode.no_image for f in row.review_flags)
    assert not any(f.code == FlagCode.no_publishable_image for f in row.review_flags)


def test_no_discard_set_is_unchanged_behaviour(local_cfg, monkeypatch):
    a = b"AAA"
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: set())
    row = CanonicalRow(src_site="x", surrogate_key="4", is_block=False, raw_image_urls=["http://x/a.jpg"])
    images.run([row], fetch=_fake_fetch({"http://x/a.jpg": a}), cfg=local_cfg)
    assert len(row.product_image_keys) == 1
    assert not any(f.code == FlagCode.no_publishable_image for f in row.review_flags)


# --------------------------------------------------------------------------- #
#  Stage 7 emit rule (passthrough): same contract, resolved via the manifest
# --------------------------------------------------------------------------- #

def test_passthrough_all_discarded_is_terminal(monkeypatch):
    h = "b" * 64
    mapped = f"https://s3/dev/products/improved/x/{h}.jpg"
    monkeypatch.setattr(images, "_readonly_manifest", lambda: {"http://x/a.jpg": mapped})
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: {h})
    cfg = ImagesConfig(mode="passthrough")
    row = CanonicalRow(src_site="x", surrogate_key="5", is_block=False, raw_image_urls=["http://x/a.jpg"])
    images.run([row], cfg=cfg)
    assert row.image_keys == []
    assert any(f.code == FlagCode.no_publishable_image for f in row.review_flags)


def test_passthrough_partial_discard_links_survivor(monkeypatch):
    h_bad, h_ok = "c" * 64, "d" * 64
    monkeypatch.setattr(images, "_readonly_manifest", lambda: {
        "http://x/a.jpg": f"https://s3/dev/products/improved/x/{h_bad}.jpg",
        "http://x/b.jpg": f"https://s3/dev/products/improved/x/{h_ok}.jpg"})
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: {h_bad})
    cfg = ImagesConfig(mode="passthrough")
    row = CanonicalRow(src_site="x", surrogate_key="6", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], cfg=cfg)
    assert row.product_image_keys == [f"https://s3/dev/products/improved/x/{h_ok}.jpg"]
    assert not any(f.code in (FlagCode.no_publishable_image, FlagCode.no_image) for f in row.review_flags)


def test_passthrough_discard_plus_held_stays_transient(monkeypatch):
    # one image discarded, one still UNtreated (held) -> not all-non-stone -> transient, retry next produce
    h_bad = "e" * 64
    monkeypatch.setattr(images, "_readonly_manifest", lambda: {
        "http://x/a.jpg": f"https://s3/dev/products/improved/x/{h_bad}.jpg",
        "http://x/b.jpg": "https://s3/dev/products/zucchi/ff.jpg"})   # on S3 but not enhanced -> held
    monkeypatch.setattr(images, "_load_discard_set", lambda cfg=None: {h_bad})
    cfg = ImagesConfig(mode="passthrough")
    row = CanonicalRow(src_site="x", surrogate_key="7", is_block=False,
                       raw_image_urls=["http://x/a.jpg", "http://x/b.jpg"])
    images.run([row], cfg=cfg)
    assert row.image_keys == []
    assert not any(f.code == FlagCode.no_publishable_image for f in row.review_flags)   # held != discarded
    assert any(f.code == FlagCode.no_image for f in row.review_flags)


# --------------------------------------------------------------------------- #
#  Classifier fail-safe: never discard when it cannot classify
# --------------------------------------------------------------------------- #

def test_classify_disabled_never_discards():
    proc = ImageProcessor(ImageProcessingConfig(enabled=True, classify=False, dewatermark=False))
    r = proc.classify(b"not-an-image")
    assert r.keep is True and r.ran is False and proc.classifier_id == ""


def test_classify_unavailable_fails_safe(monkeypatch):
    proc = ImageProcessor(ImageProcessingConfig(enabled=True, classify=True, dewatermark=False))
    monkeypatch.setattr(proc._clf, "available", lambda: False)      # deps/weights absent
    r = proc.classify(b"not-an-image")
    assert r.keep is True and r.ran is False                        # fail safe: keep, do not discard
    assert proc.classifier_id.startswith("clip:")


def test_stone_classifier_margin_and_reason():
    torch = pytest.importorskip("torch")                           # decision math needs torch (imageproc)
    from PIL import Image

    from stone_pipeline.io.image_processing import _StoneClassifier

    clf = _StoneClassifier("m", 0.85, ["s1", "s2"], ["d1", "d2"])
    clf._device = "cpu"

    class _Inp(dict):
        def to(self, device):
            return self

    clf._proc = lambda **kw: _Inp()
    # non-stone class (last two logits) clearly dominates; top non-stone prompt is d1
    clf._clip = lambda **kw: type("O", (), {"logits_per_image": torch.tensor([[0.1, 0.1, 6.0, 5.0]])})()
    keep, p_nonstone, reason = clf.classify(Image.new("RGB", (4, 4)))
    assert keep is False and p_nonstone > 0.85 and reason == "d1"

    # stone class dominates -> keep, whatever the top non-stone prompt is
    clf._clip = lambda **kw: type("O", (), {"logits_per_image": torch.tensor([[6.0, 5.0, 0.1, 0.1]])})()
    keep2, p2, _ = clf.classify(Image.new("RGB", (4, 4)))
    assert keep2 is True and p2 < 0.85
