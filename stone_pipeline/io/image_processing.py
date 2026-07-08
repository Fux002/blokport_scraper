"""Enhancement + de-watermark for scraped product photos.

These are photographs of the ACTUAL slabs a customer buys, usually shot in a
storage unit under poor, uneven light. The job is to make them presentable — fix
exposure, contrast, softness and small size — while keeping the picture a true
record of the stone (the vein pattern and colour must stay faithful; only the
watermark's footprint is reconstructed).

Enhancement is Real-ESRGAN (learned super-resolution — clean + sharpen + 4x), then
a gentle exposure lift + vibrance: brighter, crisper and natural, preserving the
stone's colour rather than desaturating it.

De-watermark reconstructs the mark's footprint with SDXL-inpaint (the classical
detect-then-copy approaches smeared patterned stone). Flagged sources only.

Bytes in, bytes out, so the image stage can apply it during re-host and tests can
inject a fake. Everything is gated by `ImageProcessingConfig` (off by default).

Pipeline per image:  de-watermark (flagged sources only) -> enhance (ESRGAN) -> beautify.

Both the ESRGAN enhancer and the SDXL de-watermark backend need the torch stack in
requirements-imageproc.txt (cv2/numpy/Pillow are core deps). When that stack is
absent each step is skipped with a single warning and the image passes through
un-enhanced — there is no classical fallback.
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
# hot path free of repeated import statements. torch/spandrel/diffusers stay lazy
# inside the enhancer/de-watermarker (optional, heavy, requirements-imageproc.txt only).

log = logfmt.get_logger("image_processing")


# --------------------------------------------------------------------------- #
#  Faithful beautify helpers (applied after Real-ESRGAN — no invented detail)
# --------------------------------------------------------------------------- #

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
#  De-watermark (locate the mark, reconstruct its footprint with SDXL inpaint)
# --------------------------------------------------------------------------- #

class _Dewatermarker:
    """Removes the consistent, semi-transparent supplier watermark from a slab photo.

    The mark is LOCATED by what stone never carries — its pink/magenta ink plus the local
    deviation of its text strokes — giving a tight central footprint. The exact stone under a
    drifting, multi-position semi-transparent mark can't be recovered (classical subtraction
    leaves artefacts, and a plain inpaint smears patterned stone), so that small footprint is
    REGENERATED with a learned inpainting model (SDXL-inpaint): natural, matching stone texture.
    A feathered composite blends it seamlessly; everything outside the footprint is untouched.

    Lazy + graceful: without torch/diffusers/weights, available() is False and de-watermarking is
    skipped (enhancement still runs). Runs fp16 on CUDA (deploy); fp32 on MPS/CPU (fp16 NaNs there)."""

    SEARCH = (0.32, 0.68, 0.22, 0.78)     # y0,y1,x0,x1 fractions of the central search band
    HUE_LO, HUE_HI, SAT_MIN = 148, 180, 35  # the mark's pink/magenta ink in OpenCV HSV
    INK_MEDIAN, INK_DELTA = 31, 9         # text strokes deviate this much from the local stone
    MIN_INK = 25                          # px of ink needed to trust that a mark is present
    PAD, FEATHER = 16, 9                  # footprint padding (px) / composite feather radius (px)
    TILE = 1024                           # SDXL native inpaint resolution
    _PROMPT = ("natural polished stone slab, continuous seamless mineral veining, "
               "photorealistic, high detail")
    _NEG = "text, letters, words, watermark, logo, sign, seam, blur, smooth patch"

    def __init__(self, model: str, steps: int, guidance: float):
        self.model, self.steps, self.guidance = model, steps, guidance
        self._ok: Optional[bool] = None
        self._pipe = None
        self._device = None

    # SDXL's stock VAE decodes to black in fp16 — use the fp16-safe VAE when running fp16 (CUDA).
    VAE_FP16 = "madebyollin/sdxl-vae-fp16-fix"

    def available(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            import torch
            from diffusers import AutoencoderKL, AutoPipelineForInpainting

            mps = getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()
            self._device = "cuda" if torch.cuda.is_available() else "mps" if mps else "cpu"
            # fp16 only on CUDA; on MPS/CPU fp16 NaN-decodes to black, so run fp32 there.
            dtype = torch.float16 if self._device == "cuda" else torch.float32
            kwargs = {"torch_dtype": dtype}
            if dtype == torch.float16:  # fp16-safe VAE, else the decode is black
                kwargs["vae"] = AutoencoderKL.from_pretrained(self.VAE_FP16, torch_dtype=dtype)
            self._pipe = AutoPipelineForInpainting.from_pretrained(self.model, **kwargs).to(self._device)
            self._pipe.set_progress_bar_config(disable=True)
            self._pipe.enable_attention_slicing()
            self._ok = True
        except Exception as exc:  # deps/weights absent — degrade gracefully
            log.warning("de-watermark unavailable; skipping (enhancement still runs)",
                        extra={"extra_fields": {"error": str(exc)}})
            self._ok = False
        return self._ok

    def _footprint(self, bgr):
        """(x0,y0,x1,y1) bounding box of the watermark, or None if no mark is found. Located
        by pink ink + local stroke deviation within the central band."""
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
        return (max(0, sx0 + xs.min() - self.PAD), max(0, sy0 + ys.min() - self.PAD),
                min(w, sx0 + xs.max() + self.PAD), min(h, sy0 + ys.max() + self.PAD))

    def process(self, pil_image):
        """Return a copy with the watermark footprint reconstructed, or the original if no mark."""
        import torch

        bgr = cv2.cvtColor(np.array(pil_image.convert("RGB")), cv2.COLOR_RGB2BGR)
        box = self._footprint(bgr)
        if box is None:
            return pil_image, False
        h, w = bgr.shape[:2]
        cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        side = (min(h, w, self.TILE) // 8) * 8                # a square crop for SDXL, /8-aligned
        x0 = int(np.clip(cx - side // 2, 0, w - side))
        y0 = int(np.clip(cy - side // 2, 0, h - side))
        crop = bgr[y0:y0 + side, x0:x0 + side]
        mcrop = np.zeros((side, side), np.uint8)
        cv2.rectangle(mcrop, (box[0] - x0, box[1] - y0), (box[2] - x0, box[3] - y0), 255, -1)

        img = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).resize((self.TILE, self.TILE))
        mask = Image.fromarray(mcrop).resize((self.TILE, self.TILE))
        result = self._pipe(
            prompt=self._PROMPT, negative_prompt=self._NEG, image=img, mask_image=mask,
            num_inference_steps=self.steps, strength=0.99, guidance_scale=self.guidance,
            generator=torch.Generator(self._device).manual_seed(0)).images[0]
        inpainted = cv2.cvtColor(np.array(result.resize((side, side))), cv2.COLOR_RGB2BGR).astype(np.float32)

        # feathered composite -> the reconstructed footprint blends with no seam
        feather = cv2.GaussianBlur(mcrop.astype(np.float32) / 255.0, (0, 0), self.FEATHER)[..., None]
        blended = crop.astype(np.float32) * (1 - feather) + inpainted * feather
        bgr[y0:y0 + side, x0:x0 + side] = np.clip(blended, 0, 255).astype(np.uint8)
        return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), True


# --------------------------------------------------------------------------- #
#  Enhancement engine: Real-ESRGAN (learned, faithful) — optional, lazy
# --------------------------------------------------------------------------- #

class _ESRGANEnhancer:
    """Real-ESRGAN learned super-resolution (clean + sharpen + 4x), loaded via spandrel. Faithful:
    reconstructs natural texture and leaves colour untouched. Lazy + graceful: if torch/spandrel/
    weights are absent, available() is False and enhancement is skipped (the image passes through
    un-enhanced — there is no fallback). Runs on CUDA (deploy), MPS (local Mac), or CPU. Large
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
        except Exception as exc:  # deps/weights absent — skip enhancement (no fallback)
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
        self._dw = (_Dewatermarker(cfg.dewatermark_model, cfg.dewatermark_steps, cfg.dewatermark_guidance)
                    if cfg.dewatermark else None)
        self._esr = _ESRGANEnhancer(cfg.esrgan_model, cfg.esrgan_weights, cfg.esrgan_tile)

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

        # 2) enhancement: Real-ESRGAN (learned clean + sharpen + 4x), then a gentle
        #    faithful beautify. If the torch stack/weights are unavailable the image
        #    passes through un-enhanced (available() logs one warning) — no fallback.
        if self._esr.available():
            bgr = self._esr.enhance(bgr)
            bgr, _ = _fit_long_edge(bgr, self.cfg.target_long_edge)      # cap the 4x output
            bgr = _levels(bgr, self.cfg.levels_lo_pct, self.cfg.levels_hi_pct)  # exposure lift
            bgr = _vibrance(bgr, self.cfg.vibrance)                      # restore muted colour
            res.enhanced = True
            res.upscaled = True

        ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        res.data = enc.tobytes() if ok else data
        return res
