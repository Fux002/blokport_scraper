"""A burned-in logo removed using the copy the hosted model has already cleaned (improved/, already paid for): that copy
is aligned to the original, only the logo's rectangle is taken from it, its tone matched to the original around it.
Everything else stays the original's own pixels and colour. A photo where this cannot be trusted is reported, never
guessed: the caller holds it.
"""
import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import marks


def _box(shape):
    h, w = shape[:2]
    bw, bh = int(C.LOGO_BOX[0] * w), int(C.LOGO_BOX[1] * w)
    return (w - bw) // 2, (h - bh) // 2, bw, bh


def _align(raw, clean):
    """clean warped onto raw, and how well they correlate (the box round the logo left out of the fit)."""
    a, b = [cv2.cvtColor(i, cv2.COLOR_RGB2GRAY).astype(np.float32) for i in (raw, clean)]
    x0, y0, bw, bh = _box(raw.shape)
    mask = np.full(a.shape, 255, np.uint8)
    mask[y0:y0 + bh, x0:x0 + bw] = 0
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, C.LOGO_ECC_STEPS, C.LOGO_ECC_EPS)
    cc, warp = cv2.findTransformECC(a, b, warp, cv2.MOTION_AFFINE, criteria, mask, 5)
    return cv2.warpAffine(clean, warp, a.shape[::-1], flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE), cc


def _rectangle(found, shape):
    """The logo as one rectangle: its pale grey word differs too little from pale stone to be found on its own."""
    joined = cv2.morphologyEx(found, cv2.MORPH_CLOSE, np.ones((C.LOGO_JOIN, C.LOGO_JOIN), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(joined)
    mask = np.zeros(shape[:2], np.uint8)
    if n > 1:
        i = 1 + int(stats[1:, cv2.CC_STAT_AREA].argmax())
        x, y, w, h = stats[i, :4]
        reach = int(C.LOGO_RIGHT * w) if w < C.LOGO_FULL * h else 0   # only the pink half was found: the word lies to its right
        mask[max(0, y - C.LOGO_PAD):y + h + C.LOGO_PAD, max(0, x - C.LOGO_PAD):x + w + reach + C.LOGO_PAD] = 1
    return mask


def _pink_left(rgb):
    """How many logo-coloured pixels are in the logo's box: what is left of the logo after removal."""
    x0, y0, bw, bh = _box(rgb.shape)
    hsv = cv2.cvtColor(rgb[y0:y0 + bh, x0:x0 + bw], cv2.COLOR_RGB2HSV)
    return int((((hsv[..., 0] > C.LOGO_HUE[0]) | (hsv[..., 0] < C.LOGO_HUE[1])) & (hsv[..., 1] > C.LOGO_MIN_SAT) & (hsv[..., 2] > C.LOGO_MIN_VAL)).sum())


def remove(raw, clean):
    """(raw without its logo, ""), or (None, the reason it cannot be trusted)."""
    h, w = raw.shape[:2]
    clean = cv2.resize(clean, (w, h), interpolation=cv2.INTER_AREA)
    try:
        aligned, cc = _align(raw, clean)
    except cv2.error as exc:
        return None, f"logo: cleaned copy could not be aligned ({exc.code})"
    if cc < C.LOGO_ALIGN_MIN:
        return None, f"logo: cleaned copy does not line up (correlation {cc:.3f})"
    lin_r, lin_c = marks._to_linear(raw).astype(np.float32), marks._to_linear(aligned).astype(np.float32)
    x0, y0, bw, bh = _box(raw.shape)
    outside = np.ones((h, w), np.float32)
    outside[y0 + C.LOGO_EDGE:y0 + bh - C.LOGO_EDGE, x0 + C.LOGO_EDGE:x0 + bw - C.LOGO_EDGE] = 0

    def local(img):
        return cv2.GaussianBlur(img * outside[..., None], (0, 0), C.LOGO_TONE_SIGMA) / \
            np.maximum(cv2.GaussianBlur(outside, (0, 0), C.LOGO_TONE_SIGMA), 1e-3)[..., None]
    matched = marks._to_srgb(lin_c * (local(lin_r) / np.maximum(local(lin_c), 1e-4))).astype(np.uint8)

    def lab(i):
        return cv2.cvtColor(i.astype(np.float32) / 255, cv2.COLOR_RGB2LAB)
    d = np.linalg.norm(lab(raw) - lab(matched), axis=2)
    found = np.zeros((h, w), np.uint8)
    found[y0:y0 + bh, x0:x0 + bw] = (d[y0:y0 + bh, x0:x0 + bw] > C.LOGO_DIFF_MIN).astype(np.uint8)
    found = cv2.morphologyEx(found, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    mask = _rectangle(found, raw.shape)
    if mask[y0:y0 + bh, x0:x0 + bw].mean() > C.LOGO_MAX_SHARE:
        return None, "logo: the copies differ across the whole box, not only at the logo"
    k = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), C.LOGO_FEATHER)[..., None]
    out = (raw * (1 - k) + matched * k).clip(0, 255).astype(np.uint8)
    if _pink_left(out) > C.LOGO_MAX_LEFT:
        return None, "logo: still visible after removal"
    return out, ""
