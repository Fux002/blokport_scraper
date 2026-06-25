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


def _lanczos_upscale(bgr, max_scale: float, target_long_edge: int):
    """Enlarge with Lanczos (high-quality, non-generative). Grows the long edge
    toward target_long_edge but never beyond max_scale and never shrinks."""
    h, w = bgr.shape[:2]
    long_edge = max(h, w)
    if long_edge >= target_long_edge:
        return bgr
    scale = min(max_scale, target_long_edge / long_edge)
    if scale <= 1.0:
        return bgr
    return cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_LANCZOS4)


# --------------------------------------------------------------------------- #
#  De-watermark (detect with Florence-2, inpaint with LaMa) — optional, lazy
# --------------------------------------------------------------------------- #

class _Dewatermarker:
    """Lazily loads Florence-2 (open-vocab detection) + LaMa (inpainting). The
    watermark varies per image, so we detect it each time rather than assume a
    fixed mask, then inpaint the detected region so the stone behind it is
    reconstructed (not blurred over). Self-hosted, free, runs on the AWS stack."""

    def __init__(self, prompt: str, ocr: bool = True):
        # prompt may be comma-separated ("logo,watermark"): detect each and union the
        # regions, so we catch both the centered branding logo and the corner info-label
        # (each prompt alone catches only one of the two).
        self.prompts = [p.strip() for p in prompt.split(",") if p.strip()] or ["watermark"]
        # OCR-with-region also marks every text overlay (info banners/labels) for removal;
        # stone carries no text, so any detected text box is an overlay, not the slab.
        self.ocr = ocr
        self._ok: Optional[bool] = None
        self._florence = None
        self._processor = None
        self._lama = None
        self._device = None

    def available(self) -> bool:
        if self._ok is not None:
            return self._ok
        try:
            self._load()
            self._ok = True
        except Exception as exc:  # deps/weights absent — degrade gracefully
            log.warning("de-watermark unavailable; skipping (enhancement still runs)",
                        extra={"extra_fields": {"error": str(exc)}})
            self._ok = False
        return self._ok

    def _load(self) -> None:
        from unittest.mock import patch

        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor
        from transformers.dynamic_module_utils import get_imports
        from simple_lama_inpainting import SimpleLama

        self._device = "cuda" if torch.cuda.is_available() else "cpu"

        # Florence-2's remote code hard-requires flash_attn (GPU-only). On CPU, strip
        # it from the import check and force eager attention so the model still loads.
        def _no_flash(filename):
            return [imp for imp in get_imports(filename) if imp != "flash_attn"]

        with patch("transformers.dynamic_module_utils.get_imports", _no_flash):
            self._florence = AutoModelForCausalLM.from_pretrained(
                "microsoft/Florence-2-large", trust_remote_code=True,
                attn_implementation="eager").to(self._device).eval()
        self._processor = AutoProcessor.from_pretrained(
            "microsoft/Florence-2-large", trust_remote_code=True)
        self._lama = SimpleLama(device=self._device)

    def _detect_boxes(self, pil_image):
        """Florence-2 open-vocab detection over every configured prompt -> unioned
        list of (x1,y1,x2,y2) boxes. Empty list = nothing found (image left as-is)."""
        import torch

        task = "<OPEN_VOCABULARY_DETECTION>"
        boxes: list = []
        for prompt in self.prompts:
            inputs = self._processor(text=task + prompt, images=pil_image,
                                     return_tensors="pt").to(self._device)
            with torch.no_grad():
                ids = self._florence.generate(input_ids=inputs["input_ids"],
                                              pixel_values=inputs["pixel_values"],
                                              max_new_tokens=1024, num_beams=3)
            text = self._processor.batch_decode(ids, skip_special_tokens=False)[0]
            parsed = self._processor.post_process_generation(
                text, task=task, image_size=(pil_image.width, pil_image.height))
            boxes.extend(parsed.get(task, {}).get("bboxes", []) or [])

        # Text overlays (the top info-banner, corner labels): OCR-with-region returns a
        # quad per text run; reduce each quad to its bounding box and add to the union.
        if self.ocr:
            ocr_task = "<OCR_WITH_REGION>"
            inputs = self._processor(text=ocr_task, images=pil_image,
                                     return_tensors="pt").to(self._device)
            with torch.no_grad():
                ids = self._florence.generate(input_ids=inputs["input_ids"],
                                              pixel_values=inputs["pixel_values"],
                                              max_new_tokens=1024, num_beams=3)
            text = self._processor.batch_decode(ids, skip_special_tokens=False)[0]
            parsed = self._processor.post_process_generation(
                text, task=ocr_task, image_size=(pil_image.width, pil_image.height))
            for quad in parsed.get(ocr_task, {}).get("quad_boxes", []) or []:
                xs, ys = quad[0::2], quad[1::2]
                boxes.append([min(xs), min(ys), max(xs), max(ys)])
        return boxes

    def process(self, pil_image):
        """Return a watermark-free copy, or the original if nothing was detected."""
        boxes = self._detect_boxes(pil_image)
        if not boxes:
            return pil_image, False
        w, h = pil_image.size
        pad = max(2, round(0.01 * min(w, h)))  # cover the mark's soft anti-aliased edges
        mask = Image.new("L", pil_image.size, 0)
        draw = ImageDraw.Draw(mask)
        for x1, y1, x2, y2 in boxes:
            draw.rectangle([max(0, x1 - pad), max(0, y1 - pad),
                            min(w, x2 + pad), min(h, y2 + pad)], fill=255)
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
        self._dw = (_Dewatermarker(cfg.dewatermark_prompt, getattr(cfg, "dewatermark_ocr", True))
                    if cfg.dewatermark else None)

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

        # 3) faithful pixel upscale (Lanczos), then a final measured sharpen to
        #    counter resampling softness
        if self.cfg.upscale:
            up = _lanczos_upscale(bgr, self.cfg.upscale_max_scale, self.cfg.upscale_target_long_edge)
            res.upscaled = up.shape[:2] != bgr.shape[:2]
            bgr = up
        if self.cfg.sharpen_amount > 0:
            bgr = _unsharp(bgr, self.cfg.sharpen_amount)

        ok, enc = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, self.cfg.jpeg_quality])
        res.data = enc.tobytes() if ok else data
        return res
