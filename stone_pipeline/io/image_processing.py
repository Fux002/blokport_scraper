"""Faithful enhancement + de-watermark for scraped product photos.

These are photographs of the ACTUAL slabs a customer buys, usually shot in a
storage unit under poor, uneven light. The job here is to make them presentable
— fix exposure, white balance, local contrast, noise and softness, and (faithfully)
enlarge small images — WITHOUT inventing any detail. No generative super-resolution:
the picture must remain a true record of the stone, or it misrepresents the
merchandise. Every step is either classical (OpenCV) or, for de-watermarking,
detect-then-inpaint — never a model that hallucinates texture.

Bytes in, bytes out, so the image stage can apply it during re-host and tests can
inject a fake. Everything is gated by `ImageProcessingConfig` (off by default).

Pipeline per image:  de-watermark (flagged sources only) -> enhance -> upscale.

The enhancement chain needs only OpenCV/numpy/Pillow (already core deps). The
de-watermark backend (Florence-2 detect + LaMa inpaint) needs the optional torch
stack in requirements-imageproc.txt; when it is absent the step is skipped with a
single warning and the rest of the chain still runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw

from stone_pipeline.config.settings import ImageProcessingConfig
from stone_pipeline.core import logfmt

# cv2/numpy/Pillow are core deps and this module is imported lazily (only when
# processing is enabled), so importing them at module top is safe and keeps the
# hot path free of repeated import statements. torch/transformers/LaMa stay lazy
# inside _Dewatermarker (optional, heavy, requirements-imageproc.txt only).

log = logfmt.get_logger("image_processing")


# --------------------------------------------------------------------------- #
#  Faithful enhancement (classical, OpenCV — no invented detail)
# --------------------------------------------------------------------------- #

def _gray_world_white_balance(bgr, max_shift: float = 0.15):
    """Neutralise a colour cast (storage-unit lighting is rarely neutral) by
    scaling each channel toward the overall grey mean. The per-channel scale is
    CLAMPED to [1-max_shift, 1+max_shift] so a genuinely coloured stone (cream
    marble, green quartzite) can't be washed grey — gray-world only corrects mild
    casts, never strongly re-tints. Faithful: re-weights colour, adds no structure."""
    means = bgr.reshape(-1, 3).mean(axis=0)
    grey = means.mean()
    scale = np.clip(grey / np.clip(means, 1e-6, None), 1.0 - max_shift, 1.0 + max_shift)
    out = bgr.astype(np.float32) * scale
    return np.clip(out, 0, 255).astype("uint8")


def _clahe_contrast(bgr, clip: float):
    """Even out exposure with contrast-limited adaptive histogram equalisation on
    the L channel only (LAB), so colour is untouched and local areas that were
    too dark/bright are normalised without blowing out the rest."""
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=max(0.1, clip), tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def _denoise(bgr, strength: int):
    # Light non-local-means; low h preserves fine veining while killing sensor grain.
    # searchWindowSize is the cost driver (NLM is ~O(searchWindow^2)); 11 instead of
    # the default 21 is ~3-4x faster per image with negligible quality loss at low h.
    return cv2.fastNlMeansDenoisingColored(bgr, None, h=strength, hColor=strength,
                                           templateWindowSize=7, searchWindowSize=11)


def _unsharp(bgr, amount: float):
    """Measured unsharp mask — recovers crispness lost to soft focus / resampling
    without the haloing of aggressive sharpening."""
    blur = cv2.GaussianBlur(bgr, (0, 0), sigmaX=1.2)
    return cv2.addWeighted(bgr, 1.0 + amount, blur, -amount, 0)


def _fit_long_edge(bgr, target_long_edge: int):
    """Cap the long edge at target_long_edge by DOWNSCALING only; never enlarge.
    Enlarging a smaller original adds bytes, not real detail — crispness comes from
    the enhancement, not pixel count (and bloated files load slowly). Returns
    (image, changed). INTER_AREA is the right filter for downscaling."""
    h, w = bgr.shape[:2]
    long_edge = max(h, w)
    if long_edge <= target_long_edge:
        return bgr, False
    scale = target_long_edge / long_edge
    return cv2.resize(bgr, (round(w * scale), round(h * scale)),
                      interpolation=cv2.INTER_AREA), True


# --------------------------------------------------------------------------- #
#  De-watermark (locate the fixed logo by colour, inpaint with LaMa) — optional
# --------------------------------------------------------------------------- #

class _Dewatermarker:
    """Removes a consistent, centred logo watermark. Florence-2 open-vocab detection
    proved unreliable on a faint, semi-transparent logo over light stone (it missed an
    unknown fraction of slabs), so we locate the mark by its distinctive HUE instead —
    the 'VARSHA STONES' logo is pink/magenta, a colour natural stone never carries — with
    a fixed central fallback that guarantees coverage when the mark is too faint to
    colour-detect (every flagged slab carries the logo). The located region is inpainted
    with LaMa so the stone behind it is reconstructed.

    Tuned for the varsha centred-logo style; the hue/region constants below are the knobs
    to retune for a different consistent watermark. No Florence/transformers needed."""

    HUE_LO, HUE_HI, SAT_MIN, VAL_MIN = 148, 180, 40, 60   # magenta/pink in OpenCV HSV (0-180)
    MIN_PINK = 25                        # pink px needed to trust a colour hit
    BAND = (0.28, 0.74, 0.18, 0.82)      # y0,y1,x0,x1 fractions of the central search band
    FALLBACK = (0.28, 0.38, 0.72, 0.62)  # x0,y0,x1,y1 fractions: fixed box when colour is faint
    # Mask the watermark's INK (its strokes), not the whole box: a solid box forces LaMa to
    # fill a large patch of clean stone, which reads as a cloudy smudge on uniform slabs.
    INK_MEDIAN = 31                      # median window estimating the stone under the thin text
    INK_DELTA = 10                       # gray deviation from that local stone = watermark ink
    MIN_INK = 300                        # too little ink found -> fall back to the solid box

    def __init__(self, *_args, **_kwargs):  # *_args kept for call-site compatibility
        self._ok: Optional[bool] = None
        self._lama = None

    def available(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            import torch
            from simple_lama_inpainting import SimpleLama

            self._lama = SimpleLama(device="cuda" if torch.cuda.is_available() else "cpu")
            self._ok = True
        except Exception as exc:  # deps/weights absent — degrade gracefully
            log.warning("de-watermark unavailable; skipping (enhancement still runs)",
                        extra={"extra_fields": {"error": str(exc)}})
            self._ok = False
        return self._ok

    def _logo_box(self, bgr):
        """(x0,y0,x1,y1) of the centred logo: located by hue, else a fixed central box."""
        h, w = bgr.shape[:2]
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        pink = ((H >= self.HUE_LO) & (H <= self.HUE_HI) &
                (S >= self.SAT_MIN) & (V >= self.VAL_MIN)).astype(np.uint8)
        by0, by1, bx0, bx1 = self.BAND
        band = np.zeros_like(pink)
        band[int(h * by0):int(h * by1), int(w * bx0):int(w * bx1)] = 1
        pink &= band
        ys, xs = np.where(pink)
        if len(xs) >= self.MIN_PINK:  # located by colour — tight box, expanded for the grey text
            x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
            bw, bh = max(x1 - x0, 1), max(y1 - y0, 1)
            return (max(0, int(x0 - 0.15 * bw)), max(0, int(y0 - 0.35 * bh)),
                    min(w, int(x1 + 0.60 * bw)), min(h, int(y1 + 1.6 * bh)))
        fx0, fy0, fx1, fy1 = self.FALLBACK
        return (int(w * fx0), int(h * fy0), int(w * fx1), int(h * fy1))

    def _ink_mask(self, bgr):
        """uint8 mask of the watermark's strokes inside the located region. Within that
        region the underlying stone is estimated by a median blur; pixels that deviate
        from it (the thin text) plus any pink ink are the mask, dilated to cover soft
        edges. Tight, so LaMa repairs only the strokes and blends — no cloudy box-fill.
        Falls back to the solid region when too little ink is found (a faint logo we must
        still remove), where a slight cloud beats a visible watermark."""
        h, w = bgr.shape[:2]
        x0, y0, x1, y1 = self._logo_box(bgr)
        mask = np.zeros((h, w), np.uint8)
        roi = bgr[y0:y1, x0:x1]
        if roi.size == 0:
            return mask
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        bg = cv2.medianBlur(gray, self.INK_MEDIAN)
        ink = cv2.absdiff(gray, bg) > self.INK_DELTA
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        pink = (hsv[:, :, 0] >= self.HUE_LO) & (hsv[:, :, 0] <= self.HUE_HI) & (hsv[:, :, 1] >= self.SAT_MIN - 5)
        roi_mask = cv2.dilate(((ink | pink) * 255).astype(np.uint8),
                              np.ones((5, 5), np.uint8), iterations=2)
        if int((roi_mask > 0).sum()) >= self.MIN_INK:
            mask[y0:y1, x0:x1] = roi_mask
        else:  # too faint to isolate -> remove the whole region to be safe
            mask[y0:y1, x0:x1] = 255
        return mask

    def process(self, pil_image):
        """Return a watermark-free copy (the located logo strokes inpainted)."""
        bgr = cv2.cvtColor(np.array(pil_image.convert("RGB")), cv2.COLOR_RGB2BGR)
        mask = Image.fromarray(self._ink_mask(bgr))
        cleaned = self._lama(pil_image.convert("RGB"), mask)
        return cleaned, True


# --------------------------------------------------------------------------- #
#  Orchestrator
# --------------------------------------------------------------------------- #

@dataclass
class ProcessResult:
    data: bytes
    dewatermarked: bool = False
    enhanced: bool = False
    upscaled: bool = False


class ImageProcessor:
    """Applies the configured chain to raw image bytes. Construct once per run.

    Never raises on a bad image: any failure logs and returns the original bytes,
    so a single corrupt photo can never crash the pipeline."""

    def __init__(self, cfg: ImageProcessingConfig):
        self.cfg = cfg
        self._dw = _Dewatermarker() if cfg.dewatermark else None  # hue-based; no prompt/OCR args

    def process(self, data: bytes, *, watermarked: bool = False) -> ProcessResult:
        if not self.cfg.enabled:
            return ProcessResult(data)
        try:
            return self._process(data, watermarked)
        except Exception as exc:  # faithful fallback: original bytes, never crash
            log.warning("image processing failed; keeping original",
                        extra={"extra_fields": {"error": str(exc)}})
            return ProcessResult(data)

    def _process(self, data: bytes, watermarked: bool) -> ProcessResult:
        res = ProcessResult(data)

        # 1) de-watermark (flagged sources only, and only if the backend loaded)
        if watermarked and self.cfg.dewatermark and self._dw and self._dw.available():
            pil = Image.open(BytesIO(data)).convert("RGB")
            cleaned, did = self._dw.process(pil)
            res.dewatermarked = did
            buf = BytesIO()
            cleaned.convert("RGB").save(buf, format="JPEG", quality=self.cfg.jpeg_quality)
            data = buf.getvalue()

        # decode for the OpenCV chain
        arr = np.frombuffer(data, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:  # not a decodable raster (svg, etc.) — leave untouched
            res.data = data
            return res

        # 2) faithful enhancement at native resolution
        if self.cfg.enhance:
            bgr = _gray_world_white_balance(bgr)
            bgr = _clahe_contrast(bgr, self.cfg.clahe_clip)
            res.enhanced = True
        if self.cfg.denoise:
            bgr = _denoise(bgr, self.cfg.denoise_strength)

        # 3) cap the long edge (downscale-only — never enlarge), then a final
        #    measured sharpen to counter any resampling softness
        if self.cfg.upscale:
            bgr, resized = _fit_long_edge(bgr, self.cfg.upscale_target_long_edge)
            res.upscaled = resized
        if self.cfg.sharpen_amount > 0:
            bgr = _unsharp(bgr, self.cfg.sharpen_amount)

        ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        res.data = enc.tobytes() if ok else data
        return res
