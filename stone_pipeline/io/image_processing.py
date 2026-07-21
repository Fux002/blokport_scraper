"""Enhancement + de-watermark for scraped product photos.

These are photographs of the ACTUAL slabs a customer buys, usually shot in a
storage unit under poor, uneven light. The job is to make them presentable -- fix
exposure, contrast, softness and small size -- while keeping the picture a true
record of the stone (the vein pattern and colour must stay faithful; only the
watermark's footprint is reconstructed).

Enhancement is Real-ESRGAN (learned super-resolution -- clean + sharpen + 4x), then
a gentle exposure lift + vibrance: brighter, crisper and natural, preserving the
stone's colour rather than desaturating it.

De-watermark locates the pink/magenta logo and sends ONLY its cropped region to FAL
FLUX Fill (hosted inpainting), then composites the fill back through the logo mask so
everything outside the logo is byte-identical -- no whole-region "box" like the old
SDXL footprint reconstruction. Flagged sources only; a failure HOLDS the image (never
publishes it watermarked).

Bytes in, bytes out, so the image stage can apply it during re-host and tests can
inject a fake. Everything is gated by `ImageProcessingConfig` (off by default).

Pipeline per image:  de-watermark (flagged sources, FAL) -> enhance (ESRGAN) -> beautify.

The ESRGAN enhancer + CLIP classifier need the torch stack in requirements-imageproc.txt
(cv2/numpy/Pillow are core deps); the FAL de-watermarker needs fal-client + FAL_KEY. When
ESRGAN's stack is absent enhancement is skipped with a warning (no fallback).
"""

from __future__ import annotations

import math
import os
import tempfile
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from stone_pipeline.config.settings import ImageProcessingConfig
from stone_pipeline.core import logfmt

# cv2/numpy/Pillow are core deps and this module is imported lazily (only when
# processing is enabled), so importing them at module top is safe and keeps the
# hot path free of repeated import statements. torch/spandrel stay lazy inside the
# enhancer (heavy, requirements-imageproc.txt only); fal_client stays lazy inside
# the de-watermarker (the FAL FLUX Fill client, network-based, no local model).

log = logfmt.get_logger("image_processing")


# --------------------------------------------------------------------------- #
#  Faithful beautify helpers (applied after Real-ESRGAN -- no invented detail)
# --------------------------------------------------------------------------- #

def _fit_long_edge(bgr, target_long_edge: int):
    """Cap the long edge at target_long_edge by DOWNSCALING only; never enlarge.
    Enlarging a smaller original adds bytes, not real detail -- crispness comes from
    the enhancement, not pixel count (and bloated files load slowly). Returns
    (image, changed). INTER_AREA is the right filter for downscaling."""
    h, w = bgr.shape[:2]
    long_edge = max(h, w)
    if long_edge <= target_long_edge:
        return bgr, False
    scale = target_long_edge / long_edge
    return cv2.resize(bgr, (round(w * scale), round(h * scale)),
                      interpolation=cv2.INTER_AREA), True


def _levels(bgr, lo_pct: float, hi_pct: float):
    """Exposure lift: stretch luminance so the lo/hi percentiles map to black/white, using the
    photo's OWN tonal range (invents nothing) -- brightens the dull, flat, badly-lit hangar shots
    so the material reads properly. Chroma is preserved (only the Y channel is stretched)."""
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    y = ycc[:, :, 0]
    lo, hi = np.percentile(y, [lo_pct, hi_pct])
    if hi <= lo:
        return bgr
    ycc[:, :, 0] = np.clip((y - lo) * 255.0 / (hi - lo), 0, 255)
    return cv2.cvtColor(ycc.astype("uint8"), cv2.COLOR_YCrCb2BGR)


def _vibrance(bgr, amount: float):
    """Restore colour that poor light muted -- boost LOW-saturation pixels more than already-colourful
    ones, so nothing over-saturates and no colour is invented, just un-muted."""
    if amount <= 0:
        return bgr
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    s = hsv[:, :, 1]
    hsv[:, :, 1] = np.clip(s * (1.0 + amount * (1.0 - s / 255.0)), 0, 255)
    return cv2.cvtColor(hsv.astype("uint8"), cv2.COLOR_HSV2BGR)


# --------------------------------------------------------------------------- #
#  De-watermark: locate the logo, send only its crop to FAL FLUX Fill
# --------------------------------------------------------------------------- #

@dataclass
class DewatermarkResult:
    image: Image.Image           # the de-watermarked image (or the original when skipped/failed)
    applied: bool = False        # the logo was found AND FAL filled it
    failed: bool = False         # FAL (or its config) failed -> caller MUST hold the image, never publish
    billed_mp: int = 0           # billed megapixels for this image (0 when no FAL call was made)


class _Dewatermarker:
    """Removes the supplier watermark with FAL FLUX Fill (hosted inpainting).

    The pink/magenta logo is LOCATED by _locate (stone never carries that ink), and ONLY the padded
    logo crop of the image is sent to FAL. The fill is composited back through a feathered logo mask,
    so every pixel outside the logo is byte-identical to the original -- no whole-region "box" like the
    old SDXL footprint reconstruction left on veined stone. FAL bills per megapixel, so cropping to the
    logo keeps each slab at the $0.05 floor; callers send the RAW (pre-upscale) image to keep the crop
    smallest.

    No local model: it needs `fal_client` + a network + `FAL_KEY`. Outcomes:
      - logo absent            -> applied=False, failed=False (clean slab, no FAL cost).
      - FAL / config failure   -> applied=False, failed=True  (HOLD the image; a re-run retries).
      - filled                 -> applied=True.
    Never returns a watermarked image marked as done."""

    SEARCH = (0.32, 0.68, 0.22, 0.78)       # y0,y1,x0,x1 fractions of the central search band
    HUE_LO, HUE_HI, SAT_MIN = 148, 180, 35  # the mark's pink/magenta ink in OpenCV HSV
    INK_MEDIAN, INK_DELTA = 31, 9           # text strokes deviate this much from the local stone
    MIN_INK = 25                            # px of ink needed to trust a mark is present
    DILATE = 4                              # grow the logo mask so FAL covers anti-aliased edges (px)
    FEATHER = 9                             # composite feather radius (px)
    MAX_RETRIES = 3
    BASE_BACKOFF = 2.0

    def __init__(self, cfg: ImageProcessingConfig):
        self.model = cfg.fal_model
        self.prompt = cfg.fal_prompt
        self.seed = cfg.fal_seed
        self.pad = cfg.crop_pad
        self.min_side = cfg.crop_min_side
        self.snap = max(1, cfg.crop_snap)
        self.price_per_mp = cfg.fal_price_per_mp
        self._ok: Optional[bool] = None

    def available(self) -> bool:
        """True only when fal_client imports AND FAL_KEY is set. False means we CANNOT de-watermark, so a
        watermarked image must be HELD (the caller treats unavailability as a failure), never published."""
        if self._ok is not None:
            return self._ok
        try:
            import fal_client  # noqa: F401
            self._ok = bool(os.environ.get("FAL_KEY"))
            if not self._ok:
                log.error("de-watermark unavailable: FAL_KEY not set (watermarked images will be HELD)")
        except Exception as exc:
            log.error("de-watermark unavailable: fal_client import failed (watermarked images will be HELD)",
                      extra={"extra_fields": {"error": str(exc)}})
            self._ok = False
        return self._ok

    def _locate(self, bgr):
        """Return (bbox, logo_mask) or None. bbox=(x0,y0,x1,y1) tight to the ink; logo_mask is a full
        image uint8 (255 = logo pixel), the SHAPE to inpaint. Presence-gated on MIN_INK ink pixels."""
        h, w = bgr.shape[:2]
        sy0, sy1, sx0, sx1 = (int(h * self.SEARCH[0]), int(h * self.SEARCH[1]),
                              int(w * self.SEARCH[2]), int(w * self.SEARCH[3]))
        roi = bgr[sy0:sy1, sx0:sx1]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        strokes = cv2.absdiff(gray, cv2.medianBlur(gray, self.INK_MEDIAN)) > self.INK_DELTA
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        pink = ((hsv[:, :, 0] >= self.HUE_LO) & (hsv[:, :, 0] <= self.HUE_HI) & (hsv[:, :, 1] >= self.SAT_MIN))
        ink = cv2.dilate(((strokes | pink) * 255).astype(np.uint8), np.ones((5, 5), np.uint8), iterations=2)
        ys, xs = np.where(ink > 0)
        if len(xs) < self.MIN_INK:
            return None
        logo_mask = np.zeros((h, w), np.uint8)
        logo_mask[sy0:sy1, sx0:sx1][ink > 0] = 255
        if self.DILATE > 0:
            k = np.ones((self.DILATE * 2 + 1, self.DILATE * 2 + 1), np.uint8)
            logo_mask = cv2.dilate(logo_mask, k)
        bbox = (int(sx0 + xs.min()), int(sy0 + ys.min()), int(sx0 + xs.max()), int(sy0 + ys.max()))
        return bbox, logo_mask

    def _pad_axis(self, a0, a1, lim):
        """Pad [a0,a1] by self.pad, grow to at least self.min_side, then round the span UP to a multiple
        of self.snap -- all clamped to [0,lim], staying valid (0 <= a0 < a1 <= lim)."""
        a0 = max(0, a0 - self.pad)
        a1 = min(lim, a1 + self.pad)
        target = min(lim, max(self.min_side, a1 - a0))
        target = min(lim, ((target + self.snap - 1) // self.snap) * self.snap)  # snap up, capped at lim
        need = target - (a1 - a0)
        if need > 0:                                    # grow symmetrically, then spill to whichever side has room
            left = min(a0, (need + 1) // 2)
            a0 -= left
            a1 = min(lim, a1 + (need - left))
            if (a1 - a0) < target:
                a0 = max(0, a1 - target)
        return a0, a1

    def _crop_box(self, bbox, w, h):
        x0, x1 = self._pad_axis(bbox[0], bbox[2], w)
        y0, y1 = self._pad_axis(bbox[1], bbox[3], h)
        return x0, y0, x1, y1

    def billed_megapixels(self, w: int, h: int) -> int:
        return max(1, math.ceil(w * h / 1_000_000))

    def process(self, pil_image) -> DewatermarkResult:
        bgr = cv2.cvtColor(np.array(pil_image.convert("RGB")), cv2.COLOR_RGB2BGR)
        located = self._locate(bgr)
        if located is None:
            return DewatermarkResult(pil_image, applied=False)      # clean slab -> no FAL cost
        bbox, logo_mask = located
        h, w = bgr.shape[:2]
        x0, y0, x1, y1 = self._crop_box(bbox, w, h)
        crop = bgr[y0:y1, x0:x1]
        cmask = logo_mask[y0:y1, x0:x1]
        if crop.size == 0 or int(cmask.max()) == 0:                 # nothing to fill after cropping
            return DewatermarkResult(pil_image, applied=False)
        try:
            filled_rgb = self._fal_fill(crop, cmask)                # RGB uint8, same HxW as crop
        except Exception as exc:                                    # network/validation/refusal -> HOLD
            log.error("de-watermark FAL failed; holding image (never publishing watermarked)",
                      extra={"extra_fields": {"error": str(exc)}})
            return DewatermarkResult(pil_image, applied=False, failed=True)
        filled_bgr = cv2.cvtColor(filled_rgb, cv2.COLOR_RGB2BGR).astype(np.float32)
        # feathered composite THROUGH the logo mask: outside-logo pixels stay byte-identical, logo blends
        feather = cv2.GaussianBlur(cmask.astype(np.float32) / 255.0, (0, 0), self.FEATHER)[..., None]
        blended = crop.astype(np.float32) * (1 - feather) + filled_bgr * feather
        bgr[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)
        out = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        return DewatermarkResult(out, applied=True, billed_mp=self.billed_megapixels(x1 - x0, y1 - y0))

    def _fal_fill(self, crop_bgr, cmask):
        """Send the crop + logo mask to FAL FLUX Fill; return the filled crop as RGB uint8 (same size).
        Retries transient errors with backoff; fails fast on deterministic (422) rejections; validates the
        output. Raises on unrecoverable failure so process() holds the image."""
        import fal_client
        import requests

        crop_rgb = Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))
        mask_l = Image.fromarray(cmask).convert("L")
        ti = tempfile.mktemp(suffix=".png")
        tm = tempfile.mktemp(suffix=".png")
        try:
            crop_rgb.save(ti)
            mask_l.save(tm)
            last = None
            for attempt in range(1, self.MAX_RETRIES + 1):
                try:
                    result = fal_client.subscribe(self.model, arguments={
                        "image_url": fal_client.upload_file(ti),
                        "mask_url": fal_client.upload_file(tm),
                        "prompt": self.prompt,
                        "seed": self.seed,
                        "output_format": "png",
                    })
                    imgs = result.get("images") or []
                    if not imgs:
                        raise ValueError(f"no images in FAL response: {list(result.keys())}")
                    url = imgs[0]["url"] if isinstance(imgs[0], dict) else imgs[0]
                    resp = requests.get(url, timeout=120)
                    resp.raise_for_status()
                    out = Image.open(BytesIO(resp.content)).convert("RGB")
                    if out.size != crop_rgb.size:                  # FLUX may round dims -> snap back
                        out = out.resize(crop_rgb.size)
                    arr = np.array(out)
                    if arr.std() < 1.0:                            # blank/black frame or a safety refusal
                        raise ValueError("FAL returned a blank image")
                    return arr
                except Exception as exc:
                    last = exc
                    msg = str(exc).lower()
                    if "422" in msg or "unprocessable" in msg:     # deterministic rejection -> no retry
                        raise
                    if attempt < self.MAX_RETRIES:
                        time.sleep(self.BASE_BACKOFF * (2 ** (attempt - 1)))
            raise last if last else RuntimeError("FAL fill failed")
        finally:
            for t in (ti, tm):
                if os.path.exists(t):
                    os.remove(t)


# --------------------------------------------------------------------------- #
#  Enhancement engine: Real-ESRGAN (learned, faithful) -- optional, lazy
# --------------------------------------------------------------------------- #

class _ESRGANEnhancer:
    """Real-ESRGAN learned super-resolution (clean + sharpen + 4x), loaded via spandrel. Faithful:
    reconstructs natural texture and leaves colour untouched. Lazy + graceful: if torch/spandrel/
    weights are absent, available() is False and enhancement is skipped (the image passes through
    un-enhanced -- there is no fallback). Runs on CUDA (deploy), MPS (local Mac), or CPU. Large
    images are tiled to bound GPU memory."""

    def __init__(self, model_name: str, weights_path: str, tile: int):
        self.model_name = model_name
        self.weights_path = weights_path
        self.tile = tile
        self._ok: Optional[bool] = None
        self._model = None
        self._device = None
        self._scale = 4

    def _resolve_weights(self) -> str:
        if self.weights_path:
            return self.weights_path
        from stone_pipeline.config.settings import REPO_ROOT
        return str(REPO_ROOT.parent / "models" / f"{self.model_name}.pth")

    def available(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            import torch
            from spandrel import ImageModelDescriptor, ModelLoader

            self._device = ("cuda" if torch.cuda.is_available()
                            else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                            else "cpu")
            model = ModelLoader().load_from_file(self._resolve_weights())
            if not isinstance(model, ImageModelDescriptor):
                raise RuntimeError("loaded file is not a single-image upscaling model")
            self._model = model.to(self._device).eval()
            self._scale = model.scale
            self._ok = True
        except Exception as exc:  # deps/weights absent -- skip enhancement (no fallback)
            log.warning("ESRGAN unavailable; skipping enhancement (image passes through)",
                        extra={"extra_fields": {"error": str(exc)}})
            self._ok = False
        return self._ok

    def _infer_tiled(self, t):
        import torch

        s = self._scale
        _, c, h, w = t.shape
        if self.tile <= 0 or (h <= self.tile and w <= self.tile):
            return self._model(t)
        ov, out_s = 32, s
        step = self.tile - ov
        out = torch.zeros(1, c, h * out_s, w * out_s, device=self._device)
        wt = torch.zeros(1, 1, h * out_s, w * out_s, device=self._device)
        for y in range(0, h, step):
            for x in range(0, w, step):
                y2, x2 = min(y + self.tile, h), min(x + self.tile, w)
                y1, x1 = max(y2 - self.tile, 0), max(x2 - self.tile, 0)
                o = self._model(t[:, :, y1:y2, x1:x2])
                out[:, :, y1 * out_s:y2 * out_s, x1 * out_s:x2 * out_s] += o
                wt[:, :, y1 * out_s:y2 * out_s, x1 * out_s:x2 * out_s] += 1
        return out / wt.clamp(min=1)

    def enhance(self, bgr):
        """BGR -> BGR, cleaned + sharpened + upscaled by the learned model."""
        import torch

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        t = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).to(self._device)
        with torch.no_grad():
            out = self._infer_tiled(t)
        arr = out.squeeze(0).clamp(0, 1).permute(1, 2, 0).cpu().numpy()
        return cv2.cvtColor((arr * 255.0 + 0.5).astype(np.uint8), cv2.COLOR_RGB2BGR)


# --------------------------------------------------------------------------- #
#  Non-stone image classifier (CLIP zero-shot) -- optional, lazy
# --------------------------------------------------------------------------- #

class _StoneClassifier:
    """Is this a photo of the stone, or a non-stone image (spec sheet, price list, plain vendor logo) that
    must never be published? CLIP scores the WHOLE image against a stone prompt set vs a non-stone set and
    discards ONLY when the non-stone class wins with probability >= margin -- conservative, because a
    discard removes the image from the catalogue. Whole-image semantic score, NOT text density: a slab
    photo carrying printed sizes, or a stone photo with a small vendor logo, is still a stone photo and is
    KEPT (its non-stone probability stays low).

    Lazy + graceful: without torch/transformers/weights, available() is False and NOTHING is discarded
    (fail safe -- never discard when we cannot classify). Deterministic (a forward pass, no sampling).
    Reuses transformers (already pulled in by the de-watermark stack); the CLIP weights are baked in."""

    def __init__(self, model: str, margin: float, stone_prompts, nonstone_prompts):
        self.model = model
        self.margin = margin
        self.stone_prompts = list(stone_prompts)
        self.nonstone_prompts = list(nonstone_prompts)
        self._ok: Optional[bool] = None
        self._clip = None
        self._proc = None
        self._device = None

    def available(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor

            self._device = ("cuda" if torch.cuda.is_available()
                            else "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
                            else "cpu")
            self._clip = CLIPModel.from_pretrained(self.model).to(self._device).eval()
            self._proc = CLIPProcessor.from_pretrained(self.model)
            self._ok = True
        except Exception as exc:  # deps/weights absent -- classify nothing (never discard blindly)
            log.warning("stone classifier unavailable; nothing discarded (images pass through)",
                        extra={"extra_fields": {"error": str(exc)}})
            self._ok = False
        return self._ok

    def classify(self, pil_image):
        """(keep: bool, p_nonstone: float, reason: str). keep is False only when the non-stone class wins
        with probability >= margin; the reason is the winning non-stone prompt (for review provenance)."""
        import torch

        prompts = self.stone_prompts + self.nonstone_prompts
        inputs = self._proc(text=prompts, images=pil_image.convert("RGB"),
                            return_tensors="pt", padding=True).to(self._device)
        with torch.no_grad():
            probs = self._clip(**inputs).logits_per_image.softmax(dim=1)[0]  # over ALL prompts
        n = len(self.stone_prompts)
        p_nonstone = float(probs[n:].sum())
        reason = self.nonstone_prompts[int(torch.argmax(probs[n:]))]
        return p_nonstone < self.margin, p_nonstone, reason


# --------------------------------------------------------------------------- #
#  Orchestrator
# --------------------------------------------------------------------------- #

@dataclass
class ProcessResult:
    data: bytes
    dewatermarked: bool = False
    enhanced: bool = False
    upscaled: bool = False
    # True iff de-watermarking a WATERMARKED image could not complete (FAL unavailable / failed / an
    # unexpected error). The caller MUST hold the image (no marker), never publish it watermarked.
    dewatermark_failed: bool = False
    billed_mp: int = 0             # FAL billed megapixels for this image (0 when no FAL call was made)


@dataclass
class ClassifyResult:
    keep: bool = True          # False only when confidently non-stone (spec sheet / logo)
    p_nonstone: float = 0.0    # probability of the non-stone class (audit / tuning)
    reason: str = ""           # the winning non-stone prompt, for review provenance
    ran: bool = False          # the classifier actually ran (deps + weights present)


class ImageProcessor:
    """Applies the configured chain to raw image bytes. Construct once per run.

    Never raises on a bad image: any failure logs and returns the original bytes,
    so a single corrupt photo can never crash the pipeline."""

    def __init__(self, cfg: ImageProcessingConfig):
        self.cfg = cfg
        self._dw = _Dewatermarker(cfg) if cfg.dewatermark else None
        self._esr = _ESRGANEnhancer(cfg.esrgan_model, cfg.esrgan_weights, cfg.esrgan_tile)
        self._clf = (_StoneClassifier(cfg.classify_model, cfg.classify_margin,
                                      cfg.classify_stone_prompts, cfg.classify_nonstone_prompts)
                     if cfg.classify else None)
        self.classifier_id = f"clip:{cfg.classify_model}" if cfg.classify else ""

    def classify(self, data: bytes) -> ClassifyResult:
        """Decide whether raw image bytes are a publishable stone photo. Never raises: any failure (or an
        absent classifier) returns keep=True (fail safe -- an unclassifiable image is never discarded)."""
        if not self.cfg.enabled or self._clf is None or not self._clf.available():
            return ClassifyResult(keep=True, ran=False)
        try:
            pil = Image.open(BytesIO(data)).convert("RGB")
            keep, p_nonstone, reason = self._clf.classify(pil)
            return ClassifyResult(keep=keep, p_nonstone=p_nonstone, reason=reason, ran=True)
        except Exception as exc:  # a bad image never forces a discard
            log.warning("classify failed; keeping image", extra={"extra_fields": {"error": str(exc)}})
            return ClassifyResult(keep=True, ran=False)

    def process(self, data: bytes, *, watermarked: bool = False, enhance: bool = True) -> ProcessResult:
        if not self.cfg.enabled:
            return ProcessResult(data)
        try:
            return self._process(data, watermarked, enhance)
        except Exception as exc:  # faithful fallback: original bytes, never crash
            log.warning("image processing failed; keeping original",
                        extra={"extra_fields": {"error": str(exc)}})
            # a failure on a WATERMARKED image must hold it (never publish the original, watermarked).
            return ProcessResult(data, dewatermark_failed=watermarked)

    def _process(self, data: bytes, watermarked: bool, enhance: bool = True) -> ProcessResult:
        res = ProcessResult(data)

        # 1) de-watermark (flagged sources only) via FAL. If the source is watermarked but de-watermarking
        #    cannot run or fails, HOLD the image (dewatermark_failed) -- never fall through to enhance+
        #    publish a watermarked slab. Keep the cleaned pixels in-memory (no intermediate JPEG).
        bgr = None
        if watermarked and self.cfg.dewatermark and self._dw is not None:
            if not self._dw.available():                      # no FAL_KEY / client -> cannot de-watermark
                res.dewatermark_failed = True
                return res
            pil = Image.open(BytesIO(data)).convert("RGB")
            dw = self._dw.process(pil)
            if dw.failed:                                     # FAL error -> hold, retry next run
                res.dewatermark_failed = True
                return res
            res.dewatermarked = dw.applied
            res.billed_mp = dw.billed_mp
            bgr = cv2.cvtColor(np.array(dw.image.convert("RGB")), cv2.COLOR_RGB2BGR)
        if bgr is None:
            bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:  # not a decodable raster (svg, etc.) -- leave untouched
                res.data = data
                return res

        # 2) enhancement: Real-ESRGAN (learned clean + sharpen + 4x), then a gentle faithful beautify --
        #    ONLY when this source has `enhance` on AND the torch stack/weights are present. When enhance is
        #    off (per-source switch) or ESRGAN is unavailable, the image is still SIZE-REDUCED (cap the long
        #    edge, CPU / cv2 -- no GPU) and re-encoded, so it is never re-hosted bloated. Either way the sha
        #    is marked processed so the publish gate treats it as done and it is not reprocessed.
        if enhance and self._esr.available():
            bgr = self._esr.enhance(bgr)
            bgr, _ = _fit_long_edge(bgr, self.cfg.target_long_edge)      # cap the 4x output
            bgr = _levels(bgr, self.cfg.levels_lo_pct, self.cfg.levels_hi_pct)  # exposure lift
            bgr = _vibrance(bgr, self.cfg.vibrance)                      # restore muted colour
            res.enhanced = True
            res.upscaled = True
        else:
            bgr, _ = _fit_long_edge(bgr, self.cfg.target_long_edge)      # CPU size-reduce only (no upscale)
            res.enhanced = True                                          # processed (size-reduced), not upscaled

        ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        res.data = enc.tobytes() if ok else data
        return res
