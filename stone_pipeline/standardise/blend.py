"""The join between a cut-out slab and the room, repainted by SDXL inpainting (the user's choice of three tried): a band
round the slab and stand, fading out across the floor, so stone and room meet naturally. The stone stays the photo's
own, the stand the one drawn stand, and a real chip stays a chip.
"""
import cv2
import numpy as np
import torch
from PIL import Image

from stone_pipeline.config.settings import STANDARDISE as C


def _repaint(img, band, pipe, seed):
    out = pipe(C.BLEND_PROMPT, negative_prompt=C.BLEND_NEGATIVE, image=Image.fromarray(img).resize(C.BLEND_SIZE, Image.LANCZOS),
               mask_image=Image.fromarray(band * 255).resize(C.BLEND_SIZE, Image.BILINEAR), width=C.BLEND_SIZE[0], height=C.BLEND_SIZE[1],
               num_inference_steps=C.BLEND_STEPS, guidance_scale=C.BLEND_GUIDANCE, strength=C.BLEND_STRENGTH,
               generator=torch.Generator("cpu").manual_seed(seed)).images[0]
    return np.asarray(out.resize((img.shape[1], img.shape[0]), Image.LANCZOS)).astype(np.float32)


def weights(solid, stand):
    """How much of the repaint is kept per pixel: all at the slab's edge, fading to none across the floor (so no
    rectangle shows where the band ends); none on the stand, and none in a chip or notch of the slab's own shape."""
    d = cv2.distanceTransform((solid == 0).astype(np.uint8), cv2.DIST_L2, 5)
    k = np.clip((C.BAND_OUT - d) / (C.BAND_FADE * C.BAND_OUT), 0, 1)
    inner = cv2.erode(solid, np.ones((2 * C.BAND_IN + 1,) * 2, np.uint8))
    k[inner > 0] = 0
    slab = ((solid > 0) & (stand == 0)).astype(np.uint8)
    hull = cv2.fillConvexPoly(np.zeros_like(slab), cv2.convexHull(cv2.findNonZero(slab)), 1) if slab.any() else slab
    chips = ((hull > 0) & (solid == 0)).astype(np.float32)
    k *= 1 - cv2.GaussianBlur(chips, (0, 0), C.BAND_CHIP_SOFT).clip(0, 1) * (chips > 0)
    return cv2.GaussianBlur(k.astype(np.float32), (0, 0), C.BLEND_FEATHER) * (stand == 0)


def blend(img, solid, stand, pipe, seed):
    """img with the join round its slab and stand repainted."""
    k = weights(solid, stand)[..., None]
    band = (k[..., 0] > 0).astype(np.uint8)
    return (img * (1 - k) + _repaint(img, band, pipe, seed) * k).clip(0, 255).astype(np.uint8)
