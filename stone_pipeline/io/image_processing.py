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
    billed_mp: int = 0           # TOTAL billed megapixels across ALL FAL generations for this image
                                 # (every retry + blank/refused generation counts, not just a final success)


class _Dewatermarker:
    """Removes the supplier watermark with FAL FLUX Kontext (hosted INSTRUCTION editing).

    Kontext edits the WHOLE image from `self.prompt` ('remove the ... logo ...') and reconstructs the stone
    underneath -- no mask, no crop. The output is resized back to the input WIDTH (aspect preserved), and a
    light sanity check HOLDS a blank or wildly-recoloured edit so a bad result is never published.

    No local model: it needs `fal_client` + a network + `FAL_KEY`. Outcomes:
      - edited                        -> applied=True.
      - FAL / config / sanity failure -> applied=False, failed=True (HOLD the image; a re-run retries).
    Never returns a watermarked image marked as done."""

    MAX_RETRIES = 3
    EDIT_MAX_SHIFT = 45                     # a valid Kontext edit keeps the slab; whole-image median colour
                                           # must not shift more than this per channel (catches a garbage edit)

    def __init__(self, cfg: ImageProcessingConfig):
        self._billed_mp = 0                     # billed megapixels accrued for the image being processed
        self.model = cfg.fal_model
        self.prompt = cfg.fal_prompt
        self.seed = cfg.fal_seed
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

    def billed_megapixels(self, w: int, h: int) -> int:
        return max(1, math.ceil(w * h / 1_000_000))

    def process(self, pil_image, prompt: Optional[str] = None) -> DewatermarkResult:
        """Remove the supplier watermark with FAL FLUX Kontext (INSTRUCTION editing -- no mask). Kontext edits
        the whole image from the instruction ('remove the ... logo ...') and reconstructs the stone underneath,
        which the old masked FLUX Fill could not do without hallucinating a label/patch on busy slabs. `prompt`
        overrides the instruction for THIS call (the per-source prompt when one processor serves many sources);
        None uses self.prompt. A light sanity check HOLDS a blank or wildly-recoloured edit so a bad result is
        never published (the caller holds a failed result and a re-run retries). Kontext re-renders at its own
        size; the output is resized back to the input WIDTH with the aspect preserved (no distortion), so the
        downstream upscale sees the same scale. Outcomes: edited -> applied=True; failure -> failed=True."""
        src = pil_image.convert("RGB")
        self._billed_mp = 0                          # accrue per FAL generation _kontext_edit submits (breaker)
        try:
            edited = self._kontext_edit(src, prompt)                     # RGB PIL at Kontext's own size
        except Exception as exc:                                         # network/validation/refusal -> HOLD
            log.error("de-watermark Kontext failed; holding image (never publishing watermarked)",
                      extra={"extra_fields": {"error": str(exc), "billed_mp": self._billed_mp}})
            return DewatermarkResult(pil_image, applied=False, failed=True, billed_mp=self._billed_mp)
        out = edited.resize((src.width, max(1, round(src.width * edited.height / edited.width))))  # aspect kept
        if not self._edit_is_sane(src, out):
            log.warning("de-watermark: Kontext edit failed sanity (blank or wild colour shift); holding image")
            return DewatermarkResult(pil_image, applied=False, failed=True, billed_mp=self._billed_mp)
        return DewatermarkResult(out, applied=True, billed_mp=self._billed_mp)

    def _kontext_edit(self, pil_rgb, prompt: Optional[str] = None):
        """Send the whole image + instruction to FAL FLUX Kontext; return the edited RGB PIL (Kontext's size).
        `prompt` overrides self.prompt for this call. Retries transient errors with backoff, fails fast on a
        deterministic (422) rejection, rejects a blank output. Raises on unrecoverable failure so process()
        holds the image. Every SUBMITTED generation bills FAL (success/blank/error alike), so it is added to
        _billed_mp per attempt -- the cost proxy the run's circuit breaker reads."""
        import fal_client
        import requests

        instruction = prompt or self.prompt
        gen_mp = self.billed_megapixels(pil_rgb.width, pil_rgb.height)   # FAL bills ~this per generation
        ti = tempfile.mktemp(suffix=".png")
        pil_rgb.save(ti)
        last = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            self._billed_mp += gen_mp                                    # every submitted generation is billed
            try:
                result = fal_client.subscribe(self.model, arguments={
                    "image_url": fal_client.upload_file(ti),
                    "prompt": instruction,
                    "seed": self.seed,
                    "output_format": "png",
                })
                imgs = result.get("images") or []
                if not imgs:
                    raise ValueError(f"no images in Kontext response: {list(result.keys())}")
                url = imgs[0]["url"] if isinstance(imgs[0], dict) else imgs[0]
                resp = requests.get(url, timeout=120)
                resp.raise_for_status()
                out = Image.open(BytesIO(resp.content)).convert("RGB")
                if float(np.asarray(out).std()) < 1.0:                   # blank/uniform -> a failed edit
                    raise ValueError("Kontext returned a blank image")
                return out
            except Exception as exc:
                last = exc
                msg = str(exc).lower()
                if "422" in msg or "unprocessable" in msg:               # deterministic rejection -> no retry
                    break
                if attempt < self.MAX_RETRIES:
                    time.sleep(2 ** attempt)
        raise last if last else RuntimeError("Kontext edit failed")

    def _edit_is_sane(self, src, out):
        """True iff the edit looks like the same slab, not a catastrophe: not blank, and the whole-image
        median colour did not shift more than EDIT_MAX_SHIFT per channel (a re-render keeps the stone's
        colour; a garbage/wrong image does not)."""
        a = np.asarray(src.resize((64, 64)).convert("RGB")).astype(np.float32).reshape(-1, 3)
        b = np.asarray(out.resize((64, 64)).convert("RGB")).astype(np.float32).reshape(-1, 3)
        if float(b.std()) < 2.0:                                         # near-uniform -> blank/failed
            return False
        shift = float(np.abs(np.median(a, 0) - np.median(b, 0)).max())
        return shift < self.EDIT_MAX_SHIFT


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
    billed_mp: int = 0             # TOTAL FAL billed megapixels across all generations (incl retries/failures)

    def is_complete(self, *, enhance_requested: bool) -> bool:
        """The image pipeline completed AS A WHOLE for what this source requires -- the ONLY condition
        under which the enhanced/ marker (which unlocks publishing to Medusa) may be written:
          - de-watermark did not fail, AND
          - the image was treated (decoded + re-encoded), AND
          - when enhance is ON, it was actually UPSCALED (Real-ESRGAN), not merely size-reduced.
        So a de-watermarked-but-not-upscaled image (e.g. a GPU run where ESRGAN was unavailable) is HELD,
        never shipped half-done; a source with enhance OFF completes on the size-reduce alone. The pipe
        goes in as a whole or not at all."""
        return ((not self.dewatermark_failed) and self.enhanced
                and (self.upscaled or not enhance_requested))


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

    def process(self, data: bytes, *, watermarked: bool = False, enhance: bool = True,
                prompt: Optional[str] = None) -> ProcessResult:
        if not self.cfg.enabled:
            return ProcessResult(data)
        try:
            return self._process(data, watermarked, enhance, prompt)
        except Exception as exc:  # faithful fallback: original bytes, never crash
            log.warning("image processing failed; keeping original",
                        extra={"extra_fields": {"error": str(exc)}})
            # a failure on a WATERMARKED image must hold it (never publish the original, watermarked).
            return ProcessResult(data, dewatermark_failed=watermarked)

    def _process(self, data: bytes, watermarked: bool, enhance: bool = True,
                 prompt: Optional[str] = None) -> ProcessResult:
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
            dw = self._dw.process(pil, prompt)               # per-source prompt override (None -> self.prompt)
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
