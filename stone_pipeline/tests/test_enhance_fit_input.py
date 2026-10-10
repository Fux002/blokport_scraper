"""Enhancement never feeds the model more pixels than the published image can show: an original larger
than `target_long_edge` is fitted to it BEFORE Real-ESRGAN, not only after. The output is capped at
target_long_edge either way, so the published picture is identical in size; the work (tiles) is bounded
by the output size instead of growing with the vendor's camera resolution (a 3,900 px original cost 48
tiles and ~80 s per photo on the GPU, 4x the budget, for pixels that were then thrown away)."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

import stone_pipeline.io.image_processing as ip
from stone_pipeline.config.settings import ImageProcessingConfig


def _jpeg(h, w):
    return cv2.imencode(".jpg", np.full((h, w, 3), 90, dtype=np.uint8))[1].tobytes()


@pytest.fixture
def fake_esrgan(monkeypatch):
    """A stand-in model: records the input it is given and returns a 4x nearest-neighbour upscale."""
    seen: list[tuple[int, int]] = []

    def enhance(self, bgr):
        seen.append(bgr.shape[:2])
        return cv2.resize(bgr, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
    monkeypatch.setattr(ip._ESRGANEnhancer, "available", lambda self: True)
    monkeypatch.setattr(ip._ESRGANEnhancer, "enhance", enhance)
    return seen


def _cfg(target):
    return ImageProcessingConfig(enabled=True, classify=False, dewatermark=False, target_long_edge=target)


def test_a_large_original_is_fitted_to_the_target_before_the_model(fake_esrgan):
    res = ip.ImageProcessor(_cfg(2048)).process(_jpeg(3000, 4000), enhance=True)
    assert fake_esrgan == [(1536, 2048)]                       # the model saw the fitted image, not 4000 px
    out = cv2.imdecode(np.frombuffer(res.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert max(out.shape[:2]) == 2048 and res.enhanced and res.upscaled


def test_a_small_original_reaches_the_model_untouched(fake_esrgan):
    res = ip.ImageProcessor(_cfg(2048)).process(_jpeg(900, 1200), enhance=True)
    assert fake_esrgan == [(900, 1200)]                        # never enlarged before the model
    out = cv2.imdecode(np.frombuffer(res.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert max(out.shape[:2]) == 2048                          # 4x -> 4800 px, capped to the target as before
