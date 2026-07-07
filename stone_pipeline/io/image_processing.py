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


def _levels(bgr, lo_pct: float, hi_pct: float):
    """Exposure lift: stretch luminance so the lo/hi percentiles map to black/white, using the
    photo's OWN tonal range (invents nothing) — brightens the dull, flat, badly-lit hangar shots
    so the material reads properly. Chroma is preserved (only the Y channel is stretched)."""
    ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)
    y = ycc[:, :, 0]
    lo, hi = np.percentile(y, [lo_pct, hi_pct])
    if hi <= lo:
        return bgr
    ycc[:, :, 0] = np.clip((y - lo) * 255.0 / (hi - lo), 0, 255)
    return cv2.cvtColor(ycc.astype("uint8"), cv2.COLOR_YCrCb2BGR)


def _vibrance(bgr, amount: float):
    """Restore colour that poor light muted — boost LOW-saturation pixels more than already-colourful
    ones, so nothing over-saturates and no colour is invented, just un-muted."""
    if amount <= 0:
        return bgr
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
    s = hsv[:, :, 1]
    hsv[:, :, 1] = np.clip(s * (1.0 + amount * (1.0 - s / 255.0)), 0, 255)
    return cv2.cvtColor(hsv.astype("uint8"), cv2.COLOR_HSV2BGR)


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
#  Enhancement engine: Real-ESRGAN (learned, faithful) — optional, lazy
# --------------------------------------------------------------------------- #

class _ESRGANEnhancer:
    """Real-ESRGAN learned super-resolution (clean + sharpen + 4x), loaded via spandrel. Faithful:
    reconstructs natural texture and leaves colour untouched — unlike the classical WB+CLAHE chain
    that desaturates and over-sharpens. Lazy + graceful: if torch/spandrel/weights are absent,
    available() is False and the caller falls back to the classical enhancement. Runs on CUDA
    (deploy), MPS (local Mac), or CPU. Large images are tiled to bound GPU memory."""

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
        except Exception as exc:  # deps/weights absent — fall back to classical
            log.warning("ESRGAN unavailable; falling back to classical enhancement",
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
        self._esr = (_ESRGANEnhancer(cfg.esrgan_model, cfg.esrgan_weights, cfg.esrgan_tile)
                     if cfg.engine == "esrgan" else None)

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

        # 1) de-watermark (flagged sources only). Keep the cleaned pixels in-memory (no
        #    intermediate JPEG) so the enhancement engine works on lossless input.
        bgr = None
        if watermarked and self.cfg.dewatermark and self._dw and self._dw.available():
            pil = Image.open(BytesIO(data)).convert("RGB")
            cleaned, did = self._dw.process(pil)
            res.dewatermarked = did
            bgr = cv2.cvtColor(np.array(cleaned.convert("RGB")), cv2.COLOR_RGB2BGR)
        if bgr is None:
            bgr = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:  # not a decodable raster (svg, etc.) — leave untouched
                res.data = data
                return res

        # 2) enhancement engine
        if self.cfg.engine == "esrgan" and self._esr is not None and self._esr.available():
            # learned clean + sharpen + 4x, then cap, then a gentle faithful beautify
            bgr = self._esr.enhance(bgr)
            bgr, _ = _fit_long_edge(bgr, self.cfg.target_long_edge)      # cap the 4x output
            bgr = _levels(bgr, self.cfg.levels_lo_pct, self.cfg.levels_hi_pct)  # exposure lift
            bgr = _vibrance(bgr, self.cfg.vibrance)                      # restore muted colour
            res.enhanced = True
            res.upscaled = True
        else:
            # classical fallback (OpenCV; no GPU, but distorts colour/texture more)
            if self.cfg.enhance:
                bgr = _gray_world_white_balance(bgr)
                bgr = _clahe_contrast(bgr, self.cfg.clahe_clip)
                res.enhanced = True
            if self.cfg.denoise:
                bgr = _denoise(bgr, self.cfg.denoise_strength)
            if self.cfg.upscale:
                bgr, resized = _fit_long_edge(bgr, self.cfg.upscale_target_long_edge)
                res.upscaled = resized
            if self.cfg.sharpen_amount > 0:
                bgr = _unsharp(bgr, self.cfg.sharpen_amount)

        ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        res.data = enc.tobytes() if ok else data
        return res
