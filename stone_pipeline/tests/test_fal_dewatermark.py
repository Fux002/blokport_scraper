"""FAL FLUX Kontext de-watermark. The de-watermark is an INSTRUCTION edit (no mask): Kontext edits the whole
image from a prompt ('remove the watermark logo') and reconstructs the stone underneath. Correctness that
matters, all with FAL mocked (no network):
  - a successful edit publishes, resized back to the input WIDTH with the aspect preserved;
  - a FAL failure (error / blank) HOLDS the image (never publishes it watermarked);
  - a garbage edit (blank / wild colour shift) is HELD by the sanity check;
  - a watermarked image is HELD whenever de-watermark cannot run or fails.
"""

from __future__ import annotations

import sys

import cv2
import numpy as np
import pytest
from PIL import Image

from stone_pipeline.config.settings import ImageProcessingConfig
from stone_pipeline.io import image_processing as ip
from stone_pipeline.io.image_processing import ImageProcessor, _Dewatermarker


def _cfg(**kw):
    return ImageProcessingConfig(dewatermark=True, enabled=True, **kw)


def _slab(h=588, w=1024):
    """A textured grey slab as a PIL RGB image (std > 0 so it is never 'blank')."""
    a = np.full((h, w, 3), 120, np.uint8)
    a[:, :, 0] = (a[:, :, 0].astype(int) + (np.arange(w) % 30)).astype(np.uint8)
    return Image.fromarray(a)


def _jpeg(pil):
    return cv2.imencode(".jpg", cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR))[1].tobytes()


# --------------------------------------------------------------------------- #
#  process(): Kontext edit -> publish / hold  (Kontext mocked)
# --------------------------------------------------------------------------- #

def test_process_edits_and_publishes(monkeypatch):
    # Kontext returns an edited image at ITS OWN size / aspect; process must publish it resized back to the
    # input WIDTH with the aspect preserved (not the input height), so there is no distortion.
    dw = _Dewatermarker(_cfg())
    edited = _slab(h=800, w=1328)   # a textured grey slab at Kontext's own size/aspect (not blank)
    monkeypatch.setattr(dw, "_kontext_edit", lambda pil: edited)
    src = _slab()
    r = dw.process(src)
    assert r.applied is True and r.failed is False
    assert r.image.width == src.width                       # resized to input width
    assert r.image.height == round(src.width * 800 / 1328)  # aspect preserved (Kontext's, not the input's)


def test_process_holds_when_kontext_fails(monkeypatch):
    dw = _Dewatermarker(_cfg())
    monkeypatch.setattr(dw, "_kontext_edit",
                        lambda pil: (_ for _ in ()).throw(RuntimeError("FAL 500")))
    r = dw.process(_slab())
    assert r.applied is False and r.failed is True          # HOLD -> caller writes no marker


def test_process_holds_a_garbage_edit(monkeypatch):
    # sanity guard: an edit that is not the same slab (blank / wildly recoloured) must be HELD, never published.
    dw = _Dewatermarker(_cfg())
    monkeypatch.setattr(dw, "_kontext_edit", lambda pil: Image.new("RGB", (1328, 800), (255, 0, 0)))  # bright red
    r = dw.process(_slab())
    assert r.applied is False and r.failed is True


def test_edit_is_sane():
    dw = _Dewatermarker(_cfg())
    src = _slab()
    bright = Image.fromarray(np.clip(np.asarray(src).astype(int) + 120, 0, 255).astype(np.uint8))  # textured shift
    assert dw._edit_is_sane(src, src) is True                                     # same slab -> sane
    assert dw._edit_is_sane(src, Image.new("RGB", src.size, (120, 120, 120))) is False  # blank -> not sane
    assert dw._edit_is_sane(src, bright) is False                                 # big colour shift -> not sane


# --------------------------------------------------------------------------- #
#  ImageProcessor: a watermarked image must be HELD when de-watermark can't run / fails
# --------------------------------------------------------------------------- #

def test_watermarked_held_when_fal_unavailable(monkeypatch):
    proc = ImageProcessor(_cfg())
    monkeypatch.setattr(proc._dw, "available", lambda: False)   # no FAL_KEY / client
    res = proc.process(_jpeg(_slab()), watermarked=True)
    assert res.dewatermark_failed is True and res.enhanced is False


def test_watermarked_held_when_dewatermark_fails(monkeypatch):
    proc = ImageProcessor(_cfg())
    monkeypatch.setattr(proc._dw, "available", lambda: True)
    monkeypatch.setattr(proc._dw, "process",
                        lambda pil: ip.DewatermarkResult(pil, applied=False, failed=True))
    res = proc.process(_jpeg(_slab()), watermarked=True)
    assert res.dewatermark_failed is True


def test_unexpected_error_on_watermarked_is_held(monkeypatch):
    # any exception in _process on a watermarked image must hold it, not return the watermarked original
    proc = ImageProcessor(_cfg())
    monkeypatch.setattr(proc, "_process", lambda *a: (_ for _ in ()).throw(ValueError("boom")))
    res = proc.process(_jpeg(_slab()), watermarked=True)
    assert res.dewatermark_failed is True
    res2 = proc.process(_jpeg(_slab()), watermarked=False)   # non-watermarked: no false hold
    assert res2.dewatermark_failed is False


# --------------------------------------------------------------------------- #
#  billed megapixels (cost breaker) + _kontext_edit blank rejection
# --------------------------------------------------------------------------- #

def test_billed_megapixels_floor_and_roundup():
    dw = _Dewatermarker(_cfg())
    assert dw.billed_megapixels(500, 400) == 1                 # 0.2 MP -> floor
    assert dw.billed_megapixels(1400, 1100) == 2               # 1.54 MP -> 2
    assert dw.billed_megapixels(2048, 2048) == 5               # 4.19 MP -> 5


def test_kontext_edit_rejects_blank_output(monkeypatch):
    # a blank/near-uniform Kontext response is a failed edit -> raises so process() HOLDS the image.
    dw = _Dewatermarker(_cfg())
    fake_fal = type("F", (), {"upload_file": staticmethod(lambda p: "url"),
                              "subscribe": staticmethod(lambda m, arguments: {"images": [{"url": "u"}]})})
    blank = np.zeros((32, 32, 3), np.uint8)
    fake_png = cv2.imencode(".png", blank)[1].tobytes()
    fake_req = type("R", (), {"get": staticmethod(
        lambda u, timeout=0: type("Resp", (), {"content": fake_png, "raise_for_status": lambda s: None})())})
    monkeypatch.setitem(sys.modules, "fal_client", fake_fal)
    monkeypatch.setitem(sys.modules, "requests", fake_req)
    monkeypatch.setattr(ip.time, "sleep", lambda *_: None)     # skip real backoff
    with pytest.raises(Exception):
        dw._kontext_edit(_slab())
