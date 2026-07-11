"""FAL FLUX Fill de-watermark: locate the pink logo, send only its crop to FAL, composite the fill back
through the logo mask. Correctness that matters most, all with FAL mocked (no network):
  - a logo-free slab is a no-op (no FAL cost);
  - the composite leaves everything OUTSIDE the logo byte-identical;
  - a FAL failure (error / blank / config) HOLDS the image (never publishes it watermarked);
  - billed megapixels + crop-box math (pad, min-side, /16, edge clamp).
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from PIL import Image

from stone_pipeline.config.settings import ImageProcessingConfig
from stone_pipeline.io import image_processing as ip
from stone_pipeline.io.image_processing import ImageProcessor, _Dewatermarker


def _cfg(**kw):
    return ImageProcessingConfig(dewatermark=True, enabled=True, **kw)


def _slab_with_logo(h=600, w=1024, logo=True):
    """A gray slab (BGR) with, optionally, a magenta 'VARSHA' logo in the central search band."""
    bgr = np.full((h, w, 3), 120, np.uint8)
    bgr[:, :, 0] += (np.arange(w) % 30).astype(np.uint8)          # a little texture so it isn't flat
    if logo:
        # magenta patch (hue ~150 in OpenCV HSV, high sat) inside SEARCH (0.32-0.68 y, 0.22-0.78 x)
        bgr[290:330, 460:560] = (200, 40, 190)                    # BGR magenta
    return bgr


def _jpeg(bgr):
    return cv2.imencode(".jpg", bgr)[1].tobytes()


# --------------------------------------------------------------------------- #
#  _locate + crop-box math
# --------------------------------------------------------------------------- #

def test_locate_finds_the_magenta_logo():
    dw = _Dewatermarker(_cfg())
    located = dw._locate(_slab_with_logo(logo=True))
    assert located is not None
    bbox, mask = located
    assert int(mask.max()) == 255 and mask.dtype == np.uint8
    x0, y0, x1, y1 = bbox
    assert 460 - 10 <= x0 <= 470 and 550 <= x1 <= 570       # tight-ish to the logo


def test_locate_returns_none_on_clean_slab():
    dw = _Dewatermarker(_cfg())
    assert dw._locate(_slab_with_logo(logo=False)) is None    # presence-gated -> no FAL cost


def test_crop_box_pads_snaps_and_clamps():
    dw = _Dewatermarker(_cfg(crop_pad=64, crop_min_side=512, crop_snap=16))
    x0, y0, x1, y1 = dw._crop_box((460, 290, 560, 330), 1024, 600)
    assert (x1 - x0) >= 512 and (y1 - y0) >= 512               # grown to min side
    assert (x1 - x0) % 16 == 0 and (y1 - y0) % 16 == 0         # /16 for FLUX
    assert 0 <= x0 < x1 <= 1024 and 0 <= y0 < y1 <= 600        # valid + clamped


def test_billed_megapixels_floor_and_roundup():
    dw = _Dewatermarker(_cfg())
    assert dw.billed_megapixels(500, 400) == 1                 # 0.2 MP -> floor
    assert dw.billed_megapixels(1400, 1100) == 2              # 1.54 MP -> 2
    assert dw.billed_megapixels(2048, 2048) == 5              # 4.19 MP -> 5


# --------------------------------------------------------------------------- #
#  process(): composite, no-op, failure-hold  (FAL mocked)
# --------------------------------------------------------------------------- #

def test_process_composites_only_the_logo(monkeypatch):
    dw = _Dewatermarker(_cfg())
    # FAL returns a solid GREEN crop; the composite must recolour only the logo region, nothing else.
    monkeypatch.setattr(dw, "_fal_fill",
                        lambda crop_bgr, cmask: np.full((crop_bgr.shape[0], crop_bgr.shape[1], 3),
                                                        (0, 255, 0), np.uint8))
    pil = Image.fromarray(cv2.cvtColor(_slab_with_logo(logo=True), cv2.COLOR_BGR2RGB))
    r = dw.process(pil)
    assert r.applied is True and r.failed is False and r.billed_mp >= 1
    out = cv2.cvtColor(np.array(r.image), cv2.COLOR_RGB2BGR)
    assert tuple(int(v) for v in out[310, 505]) != (200, 40, 190)     # logo pixel changed
    assert tuple(int(v) for v in out[10, 10]) == (120 + 10 % 30, 120, 120)  # far corner byte-identical


def test_process_noop_on_clean_slab(monkeypatch):
    dw = _Dewatermarker(_cfg())
    monkeypatch.setattr(dw, "_fal_fill",
                        lambda *a: (_ for _ in ()).throw(AssertionError("FAL must not be called on a clean slab")))
    pil = Image.fromarray(cv2.cvtColor(_slab_with_logo(logo=False), cv2.COLOR_BGR2RGB))
    r = dw.process(pil)
    assert r.applied is False and r.failed is False and r.billed_mp == 0


def test_process_holds_on_fal_failure(monkeypatch):
    dw = _Dewatermarker(_cfg())
    monkeypatch.setattr(dw, "_fal_fill",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("FAL 500")))
    pil = Image.fromarray(cv2.cvtColor(_slab_with_logo(logo=True), cv2.COLOR_BGR2RGB))
    r = dw.process(pil)
    assert r.applied is False and r.failed is True             # HOLD -> caller writes no marker


# --------------------------------------------------------------------------- #
#  ImageProcessor: a watermarked image must be HELD when FAL cannot run / fails
# --------------------------------------------------------------------------- #

def test_watermarked_held_when_fal_unavailable(monkeypatch):
    proc = ImageProcessor(_cfg())
    monkeypatch.setattr(proc._dw, "available", lambda: False)   # no FAL_KEY / client
    res = proc.process(_jpeg(_slab_with_logo()), watermarked=True)
    assert res.dewatermark_failed is True and res.enhanced is False


def test_watermarked_held_when_fal_fails(monkeypatch):
    proc = ImageProcessor(_cfg())
    monkeypatch.setattr(proc._dw, "available", lambda: True)
    monkeypatch.setattr(proc._dw, "process",
                        lambda pil: ip.DewatermarkResult(pil, applied=False, failed=True))
    res = proc.process(_jpeg(_slab_with_logo()), watermarked=True)
    assert res.dewatermark_failed is True


def test_unexpected_error_on_watermarked_is_held(monkeypatch):
    # any exception in _process on a watermarked image must hold it, not return the watermarked original
    proc = ImageProcessor(_cfg())
    monkeypatch.setattr(proc, "_process", lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    res = proc.process(_jpeg(_slab_with_logo()), watermarked=True)
    assert res.dewatermark_failed is True
    res2 = proc.process(_jpeg(_slab_with_logo()), watermarked=False)   # non-watermarked: no false hold
    assert res2.dewatermark_failed is False


# --------------------------------------------------------------------------- #
#  _fal_fill: retry / validation (fal_client + requests mocked)
# --------------------------------------------------------------------------- #

def test_fal_fill_rejects_blank_output(monkeypatch):
    dw = _Dewatermarker(_cfg())
    fake_fal = type("F", (), {"upload_file": staticmethod(lambda p: "url"),
                              "subscribe": staticmethod(lambda m, arguments: {"images": [{"url": "u"}]})})
    blank = np.zeros((32, 32, 3), np.uint8)
    fake_png = cv2.imencode(".png", blank)[1].tobytes()
    fake_req = type("R", (), {"get": staticmethod(
        lambda u, timeout=0: type("Resp", (), {"content": fake_png, "raise_for_status": lambda s: None})())})
    monkeypatch.setitem(__import__("sys").modules, "fal_client", fake_fal)
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_req)
    crop = np.full((32, 32, 3), 100, np.uint8)
    with pytest.raises(Exception):
        dw._fal_fill(crop, np.full((32, 32), 255, np.uint8))     # blank -> raises -> process holds
