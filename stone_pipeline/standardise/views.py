"""A close-up of a slab, made only from the real stone, never invented (the user: close-ups yes, corners no; the side
view is studio.place). A crop of the photo's own pixels at most 1:1 (CLOSEUP_MAX_ENLARGE), never enlarged and never given
a model's detail (the owner, 2026-11-02: a close-up must show only real detail): stored at its own size, in the canvas's
3:2 shape (the storefront's frames show it whole; Medusa copies it and makes its own thumbnails), at most the canvas's
2048 px wide, at least CLOSEUP_MIN_WIDTH; a photo without the pixels or the detail for one has none."""
import math
from dataclasses import replace

import cv2
import numpy as np

from stone_pipeline.config.settings import STANDARDISE as C
from stone_pipeline.standardise import layout
from stone_pipeline.standardise import marks as proto


def closeup_box(w, h, density=1.0):
    """(x, y, width, height) of the close-up in a w x h slab, or None when the photo has too few pixels for one. density:
    the photo's own pixels per slab pixel at the close-up, the least along either axis (_density; under 1 where the
    squaring stretched a steep or foreshortened shot). As tight as CLOSEUP_SHARE of the slab's width, widened until it
    holds CLOSEUP_MIN_WIDTH of the photo's own pixels at CLOSEUP_MAX_ENLARGE (a lower-resolution photo shows more of its
    slab), at most CLOSEUP_MAX_SHARE of the slab on either axis: past that it is the whole slab the other views already
    show."""
    least = math.ceil(C.CLOSEUP_MIN_WIDTH / (C.CLOSEUP_MAX_ENLARGE * density))
    widest = math.floor(C.CLOSEUP_MAX_SHARE * min(w, h * C.OUT_W / C.OUT_H))
    if least > widest:
        return None
    cw = min(max(round(C.CLOSEUP_SHARE * w), least), widest)
    ch = math.ceil(cw * C.OUT_H / C.OUT_W)              # rounded up: the height holds the photo's pixels the width does
    return (w - cw) // 2, (h - ch) // 2, cw, ch


def closeup_size(cw, density=1.0):
    """For a crop cw slab pixels wide, in the canvas's 3:2 shape: ((width, height) of the photo's own pixels it holds at
    CLOSEUP_MAX_ENLARGE, (width, height) it is stored at: that, made smaller to the canvas's width when wider, never
    larger)."""
    native = math.floor(cw * density * C.CLOSEUP_MAX_ENLARGE)
    shape = lambda w: (w, math.floor(w * C.OUT_H / C.OUT_W))
    return shape(native), shape(min(C.OUT_W, native))


def _density(to_slab, size):
    """The photo's own pixels per slab pixel over the widest close-up (CLOSEUP_MAX_SHARE of the slab), the least along
    either axis and at any of CLOSEUP_DENSITY_SAMPLES x CLOSEUP_DENSITY_SAMPLES points: to_slab maps a photo pixel to the
    slab of `size`. The least over the widest crop holds for every crop inside it."""
    back = np.linalg.inv(to_slab)
    w, h = size
    m = (1 - C.CLOSEUP_MAX_SHARE) / 2
    u, v = np.meshgrid(np.linspace(m * w, (1 - m) * w, C.CLOSEUP_DENSITY_SAMPLES),
                       np.linspace(m * h, (1 - m) * h, C.CLOSEUP_DENSITY_SAMPLES))
    a, b, c = back @ np.stack([u.ravel(), v.ravel(), np.ones(u.size)])
    # The homography's derivative: how far a step of one slab pixel along u or v moves in the photo.
    along_u = np.hypot(back[0, 0] * c - a * back[2, 0], back[1, 0] * c - b * back[2, 0]) / c ** 2
    along_v = np.hypot(back[0, 1] * c - a * back[2, 1], back[1, 1] * c - b * back[2, 1]) / c ** 2
    return float(min(along_u.min(), along_v.min()))


def _on_grid(f, block, n):
    """Frequencies f (of an n-point FFT) on a JPEG block grid of period `block` px: its multiples,
    CLOSEUP_JPEG_GRID_BINS bins either side."""
    return np.abs(f * block - np.round(f * block)) < C.CLOSEUP_JPEG_GRID_BINS * block / n


def fine_detail(rgb, block=C.JPEG_BLOCK):
    """The real fine detail rgb carries: across it, the local energy of the band just above half its resolution regressed
    on that of the band just below (CLOSEUP_DETAIL_BANDS), the slope. Detail of the stone's own rises with its structure;
    noise spread evenly (sensor grain, JPEG) only raises the intercept; a soft photo has little above half its
    resolution. Scored on a photo's own pixels, never resampled: the frequencies of its JPEG grid (block: its period in
    rgb's pixels) are left out. 0 when the lower band carries next to nothing (CLOSEUP_DETAIL_ENERGY_MIN: a stone smoothed
    to its 8-bit steps) or hardly varies across rgb (CLOSEUP_DETAIL_SPREAD_MIN): detail cannot then be told from noise."""
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float64)
    h, w = g.shape
    spectrum = np.fft.fft2(g - g.mean())
    fy, fx = np.fft.fftfreq(h)[:, None], np.fft.fftfreq(w)[None, :]
    radius = np.hypot(fx, fy)
    grid = _on_grid(fx, block, w) | _on_grid(fy, block, h)

    def energy(lo, hi):
        band = np.real(np.fft.ifft2(spectrum * ((radius >= lo) & (radius < hi) & ~grid))).astype(np.float32)
        return cv2.GaussianBlur(band * band, (0, 0), C.CLOSEUP_DETAIL_SMOOTH)[::C.CLOSEUP_DETAIL_STEP, ::C.CLOSEUP_DETAIL_STEP].ravel()
    below, above = (energy(*b) for b in C.CLOSEUP_DETAIL_BANDS)
    if below.mean() < C.CLOSEUP_DETAIL_ENERGY_MIN or below.std() <= C.CLOSEUP_DETAIL_SPREAD_MIN * below.mean():
        return 0.0
    return float(np.polyfit(below, above, 1)[0])


def _own_pixels(photo, to_crop, size):
    """The photo's own pixels under a crop of `size` (to_crop maps a photo pixel onto it): the axis-aligned rectangle
    inside the crop's footprint, as the photo has them (its JPEG grid on its axes)."""
    w, h = size
    corners = np.linalg.inv(to_crop) @ np.array([[0, w - 1, w - 1, 0], [0, 0, h - 1, h - 1], [1, 1, 1, 1]], np.float64)
    xs, ys = corners[0] / corners[2], corners[1] / corners[2]
    x0, x1 = max(0, math.ceil(max(xs[0], xs[3]))), min(photo.shape[1], math.floor(min(xs[1], xs[2])) + 1)
    y0, y1 = max(0, math.ceil(max(ys[0], ys[1]))), min(photo.shape[0], math.floor(min(ys[2], ys[3])) + 1)
    return np.ascontiguousarray(photo[y0:y1, x0:x1])


def _changed(flat, full):
    """Where the standardiser changed the work copy (a logo, erased labels), at its size (0/1 float), widened by a work
    pixel: a work pixel half on an erased label still shows the label in the full photo's pixels."""
    changed = np.any(flat.work != layout.fit_working(full), axis=2).astype(np.uint8)
    return cv2.dilate(changed, np.ones((3, 3), np.uint8)).astype(np.float32)


def _not_as_shot(changed, edits, size):
    """Where the work copy (of `size`) is not the photo as shot (0..1): what the standardiser changed, and `edits`, the
    start photo's own (at its size: a retouch's fills, all of a hosted model's cleaned copy)."""
    if edits is None:
        return changed
    return np.maximum(changed, cv2.resize(edits.astype(np.float32), size, interpolation=cv2.INTER_AREA))


def _crop(flat, photo, changed, to_crop, from_work, size):
    """The crop of the slab squared again from `photo`; where the standardiser changed the work copy, its own pixels."""
    crop = cv2.warpPerspective(photo, to_crop, size, flags=cv2.INTER_CUBIC)
    k = cv2.warpPerspective(changed, from_work, size, flags=cv2.INTER_LINEAR)[..., None]
    if not k.any():
        return crop
    edits = cv2.warpPerspective(flat.work, from_work, size, flags=cv2.INTER_CUBIC)
    return (crop * (1 - k) + edits * k + 0.5).astype(np.uint8)


def _light_on(light, onto, size):
    """`light`, read on the work copy's slab, mapped by `onto` onto a crop of `size`: its fields are smooth."""
    warp = lambda field: cv2.warpPerspective(field, onto, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return replace(light, flat=warp(light.flat), surround=warp(light.surround))


def _close_up_at(flat, photo, changed, not_shot, light, block):
    """(the close-up taken from `photo`, the full photo or the work copy, False), or (None, whether it was too soft):
    None when the photo has too few pixels (closeup_box), the crop is not all stone, more of it than CLOSEUP_EDIT_MAX is
    not as shot, or it is too soft to show real detail (CLOSEUP_DETAIL_MIN, judged on the photo's own pixels under the
    crop; block: the source's JPEG block in `photo`'s pixels). The crop is squared straight from the photo at the size
    it is stored at (closeup_size: never more pixels than the photo has there) and lit (the light was read on the whole
    slab)."""
    work = flat.work
    s = photo.shape[1] / work.shape[1]                  # the work copy is the full photo made smaller, evenly
    size = (round(flat.rgb.shape[1] * s), round(flat.rgb.shape[0] * s))
    from_photo = np.linalg.inv(layout.scaling(photo.shape[1] / work.shape[1], photo.shape[0] / work.shape[0]))
    to_slab = layout.scaling(s, s) @ flat.homography @ from_photo        # a photo pixel -> the slab at the photo's scale
    density = _density(to_slab, size)
    box = closeup_box(*size, density=density)
    if box is None:
        return None, False
    x, y, cw, ch = box
    if flat.alpha[int(y / s):math.ceil((y + ch) / s), int(x / s):math.ceil((x + cw) / s)].min() < 0.5:
        return None, False
    # Squared at the photo's own scale (as many pixels as the photo has there, at most 1:1), then, when that is more than
    # the stored size, made smaller with an area filter: warping straight to a smaller size samples without one, and
    # stone finer than the stored close-up shows folds into false patterns (moire).
    native, out = closeup_size(cw, density)
    onto = layout.scaling(native[0] / cw, native[1] / ch) @ np.array([[1, 0, -x], [0, 1, -y], [0, 0, 1]], np.float64) @ layout.scaling(s, s)
    from_work = onto @ flat.homography                  # the work slab -> the crop at the photo's own scale
    to_native = from_work @ from_photo
    if cv2.warpPerspective(not_shot, from_work, native, flags=cv2.INTER_LINEAR).mean() > C.CLOSEUP_EDIT_MAX:
        return None, False
    if fine_detail(_own_pixels(photo, to_native, native), block) < C.CLOSEUP_DETAIL_MIN:
        return None, True
    lit = proto.apply_light(_crop(flat, photo, changed, to_native, from_work, native), _light_on(light, onto, native))
    return (lit if native == out else cv2.resize(lit, out, interpolation=cv2.INTER_AREA)), False


def close_up(flat, full, edits=None):
    """A close-up of the real stone, or None: the middle of the slab in the canvas's shape, at most 1:1 of the photo's own
    pixels, lit as the other views are, taken from `full`, the photo the run started from at its full resolution (the
    layout works on a smaller copy). edits: where `full` itself is not as shot (photo.retouch_fills; all of a hosted
    model's cleaned copy), None when it is the photo as shot. A photo with more pixels than detail (soft at 1:1, its work
    copy sharp: phone processing) gets it from the work copy instead, under the same rules; only a too soft crop falls
    back, as one with too few pixels, not all stone or not as shot is so on the smaller copy too. A vendor's own
    enlargement passes as its pixels are, the same the main view shows: it is not told from a photo shot that way."""
    changed = _changed(flat, full)
    not_shot = _not_as_shot(changed, edits, flat.work.shape[1::-1])
    light = proto.read_light(flat.rgb, (flat.alpha > 0.5).astype(np.uint8))
    up, soft = _close_up_at(flat, full, changed, not_shot, light, C.JPEG_BLOCK)
    if soft and full.shape != flat.work.shape:
        block = C.JPEG_BLOCK * flat.work.shape[1] / full.shape[1]          # the source's grid, made smaller with it
        up, _ = _close_up_at(flat, flat.work, changed, not_shot, light, block)
    return up
