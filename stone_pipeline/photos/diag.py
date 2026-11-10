"""Why a photo's slab outline came out as it did (RUN_MODE=standardise-diag, on the GPU, dev): for each listed photo the
REAL outline step (outline.find_slabs) runs with its inner steps observed, never re-implemented, and what they saw is
written under <env>/standardise-test/diag/ and nowhere else: <sha>.json (the detector's boxes, the outline's corners, the
photo edges it runs into, each end's joint profile with its strongest joints and the trim taken, the neighbours, the
reason, SAM's proposals for every box it was prompted with), <sha>.mask.png (the slab mask the corners were fitted to),
<sha>.raw.png (what SAM counted as stone for the slab's box, before holes are filled), <sha>.straps.png (the straps
taken out of it), <sha>.proposal-<n>.png (each of SAM's proposals for the slab's box) and <sha>.overlay.jpg. Only the
survey models load.
Env: SRC; ONLY_KEY = the S3 key of a JSON list of shas (as stone_pipeline.photos.source); DIAG_TAG (optional) writes under
diag/<DIAG_TAG>/ instead; RUN_TAG (optional) reads that run's retouched copy of a photo when it has one, as its
standardise job did.
Same photos, same files: a re-run overwrites them with the same content.
"""

from __future__ import annotations

import collections
import json
import os
import re
import sys
from contextlib import contextmanager

import cv2
import numpy as np

from stone_pipeline.photos import source as source
from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client
from stone_pipeline.standardise import marks, models, outline, photo

# The inner steps of find_slabs whose calls are recorded: (module, name). find_slabs looks each up at call time.
_OBSERVED = ((outline, "detect"), (outline, "_straps"), (marks, "four_corners"), (outline, "frame_sides"),
             (outline, "_along"), (outline, "_ends"), (outline, "_neighbours"), (outline, "_partner"),
             (outline, "_outline_whole"), (marks, "slab_mask"))
_ENDS_AXES = (("top", "bottom"), ("left", "right"))     # _along trims the rows first, then the columns


@contextmanager
def observed():
    """{name: [(args, result)]} of every call to the _OBSERVED steps while the block runs, in call order; the real
    functions are put back after, whatever happened in the block."""
    calls = collections.defaultdict(list)
    real = [(module, name, getattr(module, name)) for module, name in _OBSERVED]

    def spy(name, fn):
        def call(*args, **kwargs):
            out = fn(*args, **kwargs)
            calls[name].append((args, out))
            return out
        return call
    for module, name, fn in real:
        setattr(module, name, spy(name, fn))
    try:
        yield calls
    finally:
        for module, name, fn in real:
            setattr(module, name, fn)


def _corners(q):
    return None if q is None else np.round(np.asarray(q, np.float64), 1).tolist()


def _candidates(score, lo, hi):
    """[[share, score]] of the DIAG_JOINTS strongest joints between shares lo and hi (outline._joints), by position."""
    n = len(score)
    found = sorted(outline._joints(score, lo, hi, C.DIAG_JOINT_MIN), key=lambda s: -score[round(s * n)])[:C.DIAG_JOINTS]
    return [[round(s, 4), round(float(score[round(s * n)]), 2)] for s in sorted(found)]


def _end(i, args, out):
    score, lo_at_frame, hi_at_frame = args
    lo_side, hi_side = _ENDS_AXES[i]
    return {"sides": [lo_side, hi_side], "at_frame": [bool(lo_at_frame), bool(hi_at_frame)],
            "trim": None if out is None else [round(float(v), 4) for v in out],
            "candidates": {lo_side: _candidates(score, C.JOINT_EDGE, 0.5), hi_side: _candidates(score, 0.5, 1 - C.JOINT_EDGE)},
            "profile": np.round(np.asarray(score, np.float64), 2).tolist()}


class _Sam:
    """SAM as the outline step calls it, its predicted IoU for each prompt noted in `prompts` (diagnose only)."""

    def __init__(self, sam, prompts):
        self._sam, self._prompts = sam, prompts

    def __getattr__(self, name):
        return getattr(self._sam, name)

    def __call__(self, *args, **kwargs):
        out = self._sam(*args, **kwargs)
        self._prompts[-1]["scores"] = out.iou_scores
        return out


class _PostProcess:
    """SAM's image processor, its proposals at the photo's size for each prompt noted in `prompts`."""

    def __init__(self, image_processor, prompts):
        self._ip, self._prompts = image_processor, prompts

    def __getattr__(self, name):
        return getattr(self._ip, name)

    def post_process_masks(self, *args, **kwargs):
        out = self._ip.post_process_masks(*args, **kwargs)
        self._prompts[-1]["masks"] = out[0][0]
        return out


class _Processor:
    """SAM's processor: each box or point prompt starts a new entry in `prompts` (the image-only call of embed does not)."""

    def __init__(self, proc, prompts):
        self._proc, self._prompts = proc, prompts

    def __getattr__(self, name):
        return getattr(self._proc, name)

    @property
    def image_processor(self):
        return _PostProcess(self._proc.image_processor, self._prompts)

    def __call__(self, *args, **kwargs):
        if kwargs.get("input_boxes") is not None:
            self._prompts.append({"box": [float(v) for v in kwargs["input_boxes"][0][0]]})
        elif kwargs.get("input_points") is not None:
            self._prompts.append({"box": [float(v) for v in kwargs["input_points"][0][0][0]]})
        return self._proc(*args, **kwargs)


def _proposal(prompt):
    """A prompt's proposals as JSON: its box (or point), SAM's predicted IoU and the area of each proposal."""
    return {"box": [round(v, 1) for v in prompt["box"]],
            "scores": [round(float(v), 4) for v in prompt["scores"].float().cpu()[0][0].tolist()],
            "areas": [int(v) for v in prompt["masks"].flatten(1).sum(1).tolist()]}


def _slab_images(calls, prompts, box):
    """({name: mask}, picked) of what SAM saw for the slab's own box (the box the strap step was given): the straps taken
    out (straps), its stone (raw), each of its proposals (proposal-<n>); picked = which proposal that stone came from
    (its largest piece, or the widest's pieces). ({}, None) when no slab was detected. The slab's box is prompted once; strap and neighbour probes prompt
    other boxes. Only here are the slab's proposals converted (the outline step keeps just the one it picks)."""
    if box is None:
        return {}, None
    target = [float(v) for v in box]
    images = {"straps": calls["_straps"][0][1]}
    found = next((p["masks"].numpy().astype(np.uint8) for p in prompts if p["box"] == target and "masks" in p), [])
    images.update({f"proposal-{n}": m for n, m in enumerate(found)})
    raw = next((out[1] for args, out in calls["slab_mask"] if [float(v) for v in args[2]] == target), None)
    if raw is None:
        return images, None
    images["raw"] = raw
    return images, next((n for n, m in enumerate(found) if any(np.array_equal(keep(m), raw)
                                                         for keep in (marks._largest, marks._pieces))), None)


def diagnose(seg, det, rgb, size):
    """(record, images): what find_slabs saw on rgb (the JSON record) and its masks by name: "mask" the slab mask its
    corners were fitted to, plus _slab_images ({} when no slab was detected). size = the product's (length, width) in
    metres or None, for the steep-shot check."""
    prompts = []
    sam, proc, device = seg
    with observed() as calls:
        quads, _, _, _, reason = outline.find_slabs((_Sam(sam, prompts), _Processor(proc, prompts), device), det, rgb)
    fitted = calls["four_corners"][0] if calls["four_corners"] else None    # the first fit is the slab's; later ones probe neighbours
    straps = calls["_straps"][0] if calls["_straps"] else None
    images, picked = _slab_images(calls, prompts, None if straps is None else straps[0][3])
    record = {
        "shape": list(rgb.shape[:2]), "size": list(size) if size else None, "reason": reason,
        "kind": {0: "", 1: "slab", 2: "open book"}[len(quads)],
        "detections": [[round(v, 3) for v in b] for args, out in calls["detect"] if len(args) < 3 for b in out],
        "strap_detections": [[round(v, 3) for v in b] for args, out in calls["detect"] if len(args) >= 3 for b in out],
        "box": None if straps is None else [round(float(v), 1) for v in straps[0][3]],
        "straps_px": 0 if straps is None else int(straps[1].sum()),
        "outline": None if fitted is None else _corners(fitted[1]),
        "frame_sides": [sorted(out) for _, out in calls["frame_sides"]],
        "ends": [_end(i, args, out) for i, (args, out) in enumerate(calls["_ends"])],
        "along": [[_corners(q) for q in out] for _, out in calls["_along"]],
        "neighbours": [{"side": int(d), "corners": _corners(q), "gap": round(float(g), 4)}
                       for _, out in calls["_neighbours"] for d, q, _, g in out],
        "partner": any(out is not None for _, out in calls["_partner"]),
        "outline_whole": [bool(out) for _, out in calls["_outline_whole"]],
        "quads": [_corners(q) for q in quads],
        "steep": [outline.steep_aspect(q, size) for q in quads],
        "proposals": [_proposal(p) for p in prompts if "masks" in p],
        "picked": picked,
        "settings": {k: getattr(C, k) for k in ("JOINT_EDGE", "SLIVER_MAX", "SEAM_MIN_END", "FRAME_EDGE", "FRAME_TOUCH",
                                                 "EXTEND_MAX")},
    }
    if fitted is not None:
        images["mask"] = fitted[0][0]
    return record, images


def _line(inv, size, share, columns):
    """The photo's points of a line at `share` of the squared outline (size = its (w, h)): a column or a row."""
    w, h = size
    pts = [[share * (w - 1), 0], [share * (w - 1), h - 1]] if columns else [[0, share * (h - 1)], [w - 1, share * (h - 1)]]
    return cv2.perspectiveTransform(np.float32([pts]), inv)[0]


def overlay(rgb, record, mask):
    """The photo with what find_slabs saw drawn on it (DIAG_COLOURS): the mask tinted, the
    detector's boxes (the one used thick), the fitted outline, the photo edges it runs into, each end's joints (with
    their score) and the trims taken, the slabs kept."""
    col, wide = C.DIAG_COLOURS, C.DIAG_WIDTHS
    img = rgb.astype(np.float32)
    if mask is not None:
        img[mask > 0] = img[mask > 0] * (1 - C.DIAG_TINT) + np.float32(col["mask"]) * C.DIAG_TINT
    img = img.astype(np.uint8).copy()
    t = max(C.DIAG_LINE_MIN, max(rgb.shape[:2]) // C.DIAG_LINE_PER)
    for x0, y0, x1, y1, _ in record["detections"]:
        cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), col["detection"], t)
    if record["box"]:
        x0, y0, x1, y1 = record["box"]
        cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), col["box"], wide["box"] * t)
    h, w = rgb.shape[:2]
    b = wide["frame"] * t
    bands = {"left": (0, 0, b, h), "right": (w - b, 0, w, h), "top": (0, 0, w, b), "bottom": (0, h - b, w, h)}
    for side in (record["frame_sides"][0] if record["frame_sides"] else []):
        x0, y0, x1, y1 = bands[side]
        cv2.rectangle(img, (x0, y0), (x1, y1), col["frame"], -1)
    if record["outline"]:
        q = np.float32(record["outline"])
        cv2.polylines(img, [q.astype(np.int32)], True, col["outline"], wide["outline"] * t)
        flat, H = outline._flat(rgb, q)
        inv, size = np.linalg.inv(H), flat.shape[1::-1]
        for end, columns in zip(record["ends"], (False, True)):
            for side, found in end["candidates"].items():
                for share, score in found:
                    a, b2 = _line(inv, size, share, columns)
                    cv2.line(img, tuple(map(int, a)), tuple(map(int, b2)), col["joint"], t)
                    cv2.putText(img, f"{score:.1f}", tuple(map(int, (a + b2) / 2)), cv2.FONT_HERSHEY_SIMPLEX, t * C.DIAG_FONT, col["joint"], t)
            for share in end["trim"] or []:
                a, b2 = _line(inv, size, share, columns)
                cv2.line(img, tuple(map(int, a)), tuple(map(int, b2)), col["trim"], wide["trim"] * t)
    for q in record["quads"]:
        cv2.polylines(img, [np.int32(q)], True, col["slab"], wide["slab"] * t)
    tx, ty = C.DIAG_TEXT_AT
    cv2.putText(img, record["reason"] or record["kind"], (tx * t, ty * t), cv2.FONT_HERSHEY_SIMPLEX, t * C.DIAG_FONT, col["text"], t)
    s = C.DIAG_LONG_EDGE / max(h, w)
    return cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else img


def _png(mask):
    ok, buf = cv2.imencode(".png", (mask > 0).astype(np.uint8) * 255)
    if not ok:
        raise ValueError("could not encode the mask")
    return buf.tobytes()


def diag_prefix():
    """Where the diagnostic writes: <env>/standardise-test/diag/, under DIAG_TAG/ when set (two runs side by side)."""
    tag = os.environ.get("DIAG_TAG", "").strip()
    if tag and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", tag):
        raise ValueError(f"DIAG_TAG must be lowercase letters, digits and dashes: {tag!r}")
    return f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{C.DIAG_DIR}/{tag + '/' if tag else ''}"


def write(client, sha, record, rgb, images):
    """The photo's diagnostic files under diag_prefix(): the record, each of its masks (diagnose) and the overlay."""
    out = f"{diag_prefix()}{sha}"
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}.json", Body=json.dumps(record, sort_keys=True).encode(), ContentType="application/json")
    for name, mask in sorted(images.items()):
        client.put_object(Bucket=S3_BUCKET, Key=f"{out}.{name}.png", Body=_png(mask), ContentType="image/png")
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}.overlay.jpg", Body=source._encode(overlay(rgb, record, images.get("mask"))),
                      ContentType="image/jpeg")


def main() -> int:
    src = os.environ.get("SRC", "").strip()
    if not src or not os.environ.get("ONLY_KEY", "").strip():
        print("ERROR: SRC and ONLY_KEY are required; aborting")
        return 2
    client = aws_client("s3", config=BOTOCORE_DEFAULTS)
    wanted = source.listed_shas(client)
    keys = source.only(source.scraped_keys(client, src), wanted)
    missing = sorted(set(wanted) - {imagestore.sha_from_url(k) for k in keys})
    sheet = source.sheet_photos(client)
    m = models.load("survey")
    failed = []
    print(f"==> outline diagnostic {src}: {len(keys)} photos -> {diag_prefix()}", flush=True)
    for key in keys:
        sha = imagestore.sha_from_url(key)
        try:
            start, _, refused = source.start(client, source.run_prefix(src), key, sha)
            read = start or source.photo_source(client, source.run_prefix(src), key, sha)
            rgb = photo.decode(source._get(client, read))
            info = sheet.get(sha, {})
            size = tuple(info["size"]) if info.get("size") else None
            record, images = diagnose(m.seg, m.det, rgb, size)
            record.update(sha=sha, photo=read, retouch_refused=refused)
            write(client, sha, record, rgb, images)
            del images                                 # the photo's proposals (3 photo-sized masks) go before the next is read
            print(f"   {sha[:12]} {record['reason'] or record['kind']}", flush=True)
        except Exception as exc:                       # one bad photo never stops the others; the job still fails at the end
            failed.append(sha)
            print(f"   FAILED {sha[:12]}: {type(exc).__name__}: {exc}", flush=True)
    if missing:
        print(f"ERROR: {len(missing)} listed photos are not scraped for {src}: {', '.join(s[:12] for s in missing)}", flush=True)
    print(f"==> {src} diagnostic done: {len(keys) - len(failed)} written, {len(failed)} failed, {len(missing)} missing", flush=True)
    return 1 if failed or missing else 0


if __name__ == "__main__":
    sys.exit(main())
