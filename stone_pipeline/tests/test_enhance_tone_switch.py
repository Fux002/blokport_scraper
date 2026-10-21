"""The tone steps after Real-ESRGAN (exposure lift + vibrance) are one switch, ON by default: the stone
recipe is unchanged. Off gives a detail-only enhance (Real-ESRGAN 4x + the long-edge cap, the photo's own
tones kept), for bright studio renders that the percentile stretch would darken. SCRAPER_IMAGE_TONE=false
(or the legacy BLOKPORT_IMAGE_TONE) turns it off for a deployment or a single Batch job."""

from __future__ import annotations

import os
import subprocess
import sys

import cv2
import numpy as np
import pytest

import stone_pipeline.io.image_processing as ip
from stone_pipeline.config.settings import ImageProcessingConfig


def test_the_default_recipe_is_unchanged():
    cfg = ImageProcessingConfig()
    assert cfg.tone is True
    assert (cfg.levels_lo_pct, cfg.levels_hi_pct, cfg.vibrance, cfg.target_long_edge) == (0.5, 99.6, 0.20, 2048)


@pytest.fixture
def steps(monkeypatch):
    """A stand-in model (4x nearest) and recorders for the two tone steps."""
    calls: list[str] = []
    monkeypatch.setattr(ip._ESRGANEnhancer, "available", lambda self: True)
    monkeypatch.setattr(ip._ESRGANEnhancer, "enhance",
                        lambda self, bgr: cv2.resize(bgr, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST))
    monkeypatch.setattr(ip, "_levels", lambda bgr, lo, hi: calls.append("levels") or bgr)
    monkeypatch.setattr(ip, "_vibrance", lambda bgr, amount: calls.append("vibrance") or bgr)
    return calls


def _process(tone):
    image = cv2.imencode(".jpg", np.full((600, 800, 3), 230, dtype=np.uint8))[1].tobytes()
    cfg = ImageProcessingConfig(enabled=True, classify=False, dewatermark=False, tone=tone)
    return ip.ImageProcessor(cfg).process(image, enhance=True)


def test_tone_on_lifts_exposure_and_vibrance_after_the_model(steps):
    res = _process(tone=True)
    assert steps == ["levels", "vibrance"] and res.enhanced and res.upscaled


def test_tone_off_is_detail_only_model_and_cap_kept(steps):
    res = _process(tone=False)
    out = cv2.imdecode(np.frombuffer(res.data, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert steps == [] and res.enhanced and res.upscaled
    assert max(out.shape[:2]) == 2048                              # 4x, capped at the long edge as before


@pytest.mark.parametrize("name", ["SCRAPER_IMAGE_TONE", "BLOKPORT_IMAGE_TONE"])
def test_the_env_switch_turns_tone_off(name):
    env = {k: v for k, v in os.environ.items() if not k.endswith("IMAGE_TONE")}
    env[name] = "false"
    out = subprocess.run([sys.executable, "-c", "from stone_pipeline.config.settings import "
                          "ImageProcessingConfig as C; print(C().tone)"],
                         capture_output=True, text=True, env=env, check=True)
    assert out.stdout.strip() == "False"
