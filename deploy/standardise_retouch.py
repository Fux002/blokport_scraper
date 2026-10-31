"""Take straps, racks, sample cards, stickers and writing off a source's photos (RUN_MODE=standardise-retouch, GPU), ahead of
deploy.standardise_source, which then starts from the result. Two phases, because the machine holds one set of models at a
time: find the marks on every photo, free those models, then edit. Writes <run prefix>/retouched/<sha>.jpg, or
retouch_held/<sha>.json when a result is refused. A test run (RUN_TAG) also writes what was found to retouch_marks/<sha>.jpg,
for a person to check, and under retouch_debug/<sha>/ the masks and every model output in the order the crops were edited
(retouch.regions), so the checks can be tuned on what the model really does.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time

import boto3
import cv2
import numpy as np
import torch
from PIL import Image

from deploy.standardise_source import _encode, _get, _photo_keys, run_prefix
from stone_pipeline.config.settings import S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import marks, outline, photo, retouch


def _write_found(client, out, sha, rgb, straps, cards):
    shown = rgb.copy()
    shown[straps > 0] = (shown[straps > 0] * 0.5 + np.array((255, 0, 0)) * 0.5).astype(np.uint8)
    shown[cards > 0] = (shown[cards > 0] * 0.5 + np.array((0, 255, 0)) * 0.5).astype(np.uint8)
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}retouch_marks/{sha}.jpg", Body=_encode(shown, 80), ContentType="image/jpeg")


class _Recorder:
    """A test run's record of one photo: its masks and each model output, written as they come."""

    def __init__(self, client, out, sha):
        self.client, self.prefix, self.n = client, f"{out}retouch_debug/{sha}/", 0

    def put(self, name, img, png=False):
        data = cv2.imencode(".png", img)[1].tobytes() if png else _encode(img, 95)
        self.client.put_object(Bucket=S3_BUCKET, Key=f"{self.prefix}{name}", Body=data, ContentType="image/png" if png else "image/jpeg")

    def wrap(self, edit, kind):
        def recorded(crop):
            result = edit(crop)
            self.put(f"{kind}_{self.n:02d}.jpg", result)
            self.n += 1
            return result
        return recorded


class _NoRecord:
    def put(self, name, img, png=False):
        pass

    def wrap(self, edit, kind):
        return edit


def _pack(mask):
    # A full slice holds a mask set per photo in a 16 GB machine until the edit phase: kept compressed.
    return cv2.imencode(".png", mask)[1].tobytes()


def _unpack(data):
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)


def _find(client, keys, out):
    """{key: packed (straps, cards, stone)} of every photo with a mark on it."""
    seg = marks.load_sam()
    det = outline.load_detector(seg[2])
    found = {}
    for key in keys:
        sha = imagestore.sha_from_url(key)
        try:
            rgb = photo.decode(_get(client, key))
            straps, cards, stone = retouch.find_marks(seg, det, rgb)
            if not (straps.any() or cards.any()):
                continue
            found[key] = (_pack(straps), _pack(cards), _pack(stone))
            if os.environ.get("RUN_TAG"):
                _write_found(client, out, sha, rgb, straps, cards)
        except Exception as exc:                       # one bad photo never stops the slice
            print(f"   FIND FAILED {sha[:12]}: {type(exc).__name__}: {exc}", flush=True)
    del seg, det
    gc.collect()
    torch.cuda.empty_cache()
    return found


def _load_editor():
    """The model, as a function: crop to edited crop."""
    from diffusers import Flux2KleinPipeline
    encoder = Flux2KleinPipeline.from_pretrained(C.EDIT_DIR, transformer=None, vae=None, torch_dtype=torch.float16, low_cpu_mem_usage=True).to("cuda")
    with torch.no_grad():
        embed = encoder.encode_prompt(prompt=C.RETOUCH_PROMPT, device="cuda", max_sequence_length=C.EDIT_MAX_TOKENS)[0].cpu()
    del encoder
    gc.collect()
    torch.cuda.empty_cache()
    pipe = Flux2KleinPipeline.from_pretrained(C.EDIT_DIR, text_encoder=None, tokenizer=None, torch_dtype=torch.float16, low_cpu_mem_usage=True).to("cuda")

    def edit(crop):
        w, h = retouch.model_size(crop.shape[1], crop.shape[0])
        return np.asarray(pipe(prompt_embeds=embed.to("cuda"), image=Image.fromarray(crop).resize((w, h), Image.LANCZOS), width=w, height=h,
                               num_inference_steps=C.EDIT_STEPS, guidance_scale=C.EDIT_GUIDANCE,
                               generator=torch.Generator("cpu").manual_seed(C.BLEND_SEED)).images[0])
    return edit


def _hold(client, out, sha, reason, straps_only):
    body = {"reason": reason, "straps_only": straps_only}
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}retouch_held/{sha}.json", Body=json.dumps(body, sort_keys=True).encode(), ContentType="application/json")


def main() -> int:
    src = os.environ.get("SRC", "").strip()
    if not src:
        print("ERROR: SRC is required; aborting")
        return 2
    client = boto3.client("s3", region_name=S3_REGION)
    out = run_prefix(src)
    keys = _photo_keys(client, src)
    found = _find(client, keys, out)
    print(f"==> retouch {src}: marks on {len(found)} of {len(keys)} photos", flush=True)
    if not found:
        return 0
    edit = _load_editor()
    done = refused = 0
    for key, packed in found.items():
        sha = imagestore.sha_from_url(key)
        started = time.time()
        try:
            straps, cards, stone = (_unpack(p) for p in packed)
            rgb = photo.decode(_get(client, key))
            rec = _Recorder(client, out, sha) if os.environ.get("RUN_TAG") else _NoRecord()
            for name, mask in (("straps", straps), ("cards", cards), ("stone", stone)):
                rec.put(f"{name}.png", mask * 255, png=True)
            merged, reason = retouch.retouch(rgb, straps | cards, stone, rec.wrap(edit, "marks"))
            if merged is None:
                _hold(client, out, sha, reason, not cards.any())
                refused += 1
                print(f"   {sha[:12]} refused: {reason} {time.time() - started:.1f}s", flush=True)
                continue
            client.put_object(Bucket=S3_BUCKET, Key=f"{out}retouched/{sha}.jpg", Body=_encode(merged, 95), ContentType="image/jpeg")
            done += 1
            print(f"   {sha[:12]} retouched {time.time() - started:.1f}s", flush=True)
        except Exception as exc:                       # one bad photo never stops the slice
            print(f"   FAILED {sha[:12]}: {type(exc).__name__}: {exc}", flush=True)
    print(f"==> retouch {src} done: {done} retouched, {refused} refused", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
