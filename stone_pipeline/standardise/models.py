"""The standardiser's models, loaded once per job, all on our own GPU (fail loud when one is missing: no silent soft output)."""
import os
from dataclasses import dataclass
from typing import Any

import torch

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import marks, outline


@dataclass
class Models:
    seg: Any
    det: Any
    lama: Any
    esr: Any
    clip: Any
    inpaint: Any

    @property
    def cleaners(self):
        """What the slab steps take: segmenter, detector, eraser, upscaler."""
        return self.seg, self.det, self.lama, self.esr


def device():
    return "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"


def _esrgan():
    from stone_pipeline.io.image_processing import _ESRGANEnhancer
    weights = os.environ.get("BLOKPORT_ESRGAN_WEIGHTS", "")
    esr = _ESRGANEnhancer("RealESRGAN_x4plus", weights, C.ESRGAN_TILE)
    if not esr.available():
        raise RuntimeError(f"Real-ESRGAN did not load from {weights!r}")
    return esr


def _clip(dev):
    from transformers import CLIPModel, CLIPProcessor
    return CLIPModel.from_pretrained(C.CLIP_MODEL, revision=C.CLIP_REVISION).to(dev).eval(), CLIPProcessor.from_pretrained(C.CLIP_MODEL, revision=C.CLIP_REVISION), dev


def _inpaint(dev):
    from diffusers import AutoencoderKL, StableDiffusionXLInpaintPipeline
    vae = AutoencoderKL.from_pretrained(C.BLEND_VAE, revision=C.BLEND_VAE_REVISION, torch_dtype=torch.float16)
    return StableDiffusionXLInpaintPipeline.from_pretrained(C.BLEND_MODEL, revision=C.BLEND_REVISION, vae=vae, torch_dtype=torch.float16, variant="fp16",
                                                            use_safetensors=True).to(dev)


def load(survey_only=False):
    """All the models; survey_only: just what classifying a photo needs (cheap, no eraser, upscaler or inpainting)."""
    from simple_lama_inpainting import SimpleLama
    dev = device()
    seg = marks.load_sam()
    det = outline.load_detector(seg[2])
    clip = _clip(dev)
    if survey_only:
        return Models(seg, det, None, None, clip, None)
    return Models(seg, det, SimpleLama(), _esrgan(), clip, _inpaint(dev))
