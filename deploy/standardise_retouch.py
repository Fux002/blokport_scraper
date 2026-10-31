"""Take straps, sample cards and stickers off a source's photos (RUN_MODE=standardise-retouch, GPU), ahead of deploy.standardise_source,
which then starts from the result. Two phases, because the machine holds one set of models at a time: find the marks on
every photo, free those models, then edit. Writes <run prefix>/retouched/<sha>.jpg, or retouch_held/<sha>.json when a result
is refused. A photo with nothing to take off is never sent to the model.
"""

from __future__ import annotations

import gc
import json
import os
import sys
import time

import boto3
import numpy as np
import torch
from PIL import Image

from deploy.standardise_source import _encode, _get, _photo_keys, run_prefix
from stone_pipeline.config.settings import S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import marks, outline, photo, retouch


def _size(w, h):
    k = (C.EDIT_PIXELS / (w * h)) ** 0.5
    return int(w * k) // 16 * 16, int(h * k) // 16 * 16


def _find(client, keys):
    seg = marks.load_sam()
    det = outline.load_detector(seg[2])
    found = {}
    for key in keys:
        sha = imagestore.sha_from_url(key)
        try:
            rgb = photo.decode(_get(client, key))
            straps, cards = retouch.find_marks(seg, det, rgb)
            if straps.any() or cards.any():
                found[key] = (straps, cards)
        except Exception as exc:                       # one bad photo never stops the slice
            print(f"   FIND FAILED {sha[:12]}: {type(exc).__name__}: {exc}", flush=True)
    del seg, det
    gc.collect()
    torch.cuda.empty_cache()
    return found


def _load_editor():
    from diffusers import Flux2KleinPipeline
    encoder = Flux2KleinPipeline.from_pretrained(C.EDIT_DIR, transformer=None, vae=None, torch_dtype=torch.float16, low_cpu_mem_usage=True).to("cuda")
    with torch.no_grad():
        embed = encoder.encode_prompt(prompt=C.RETOUCH_PROMPT, device="cuda", max_sequence_length=C.EDIT_MAX_TOKENS)[0].cpu()
    del encoder
    gc.collect()
    torch.cuda.empty_cache()
    return Flux2KleinPipeline.from_pretrained(C.EDIT_DIR, text_encoder=None, tokenizer=None, torch_dtype=torch.float16, low_cpu_mem_usage=True).to("cuda"), embed


def main() -> int:
    src = os.environ.get("SRC", "").strip()
    if not src:
        print("ERROR: SRC is required; aborting")
        return 2
    client = boto3.client("s3", region_name=S3_REGION)
    out = run_prefix(src)
    keys = _photo_keys(client, src)
    found = _find(client, keys)
    print(f"==> retouch {src}: marks on {len(found)} of {len(keys)} photos", flush=True)
    if not found:
        return 0
    pipe, embed = _load_editor()
    done = refused = 0
    for key, (straps, cards) in found.items():
        sha = imagestore.sha_from_url(key)
        started = time.time()
        try:
            rgb = photo.decode(_get(client, key))
            w, h = _size(rgb.shape[1], rgb.shape[0])
            edit = pipe(prompt_embeds=embed.to("cuda"), image=Image.fromarray(rgb).resize((w, h), Image.LANCZOS), width=w, height=h,
                        num_inference_steps=C.EDIT_STEPS, guidance_scale=C.EDIT_GUIDANCE,
                        generator=torch.Generator("cpu").manual_seed(C.BLEND_SEED)).images[0]
            merged, reason = retouch.merge(rgb, np.asarray(edit), straps | cards)
            if merged is None:
                body = {"reason": reason, "straps_only": not cards.any()}
                client.put_object(Bucket=S3_BUCKET, Key=f"{out}retouch_held/{sha}.json", Body=json.dumps(body, sort_keys=True).encode(), ContentType="application/json")
                refused += 1
            else:
                client.put_object(Bucket=S3_BUCKET, Key=f"{out}retouched/{sha}.jpg", Body=_encode(merged, 95), ContentType="image/jpeg")
                done += 1
            print(f"   {sha[:12]} {'refused: ' + reason if merged is None else 'ok'} {time.time() - started:.1f}s", flush=True)
        except Exception as exc:                       # one bad photo never stops the slice
            print(f"   FAILED {sha[:12]}: {type(exc).__name__}: {exc}", flush=True)
    print(f"==> retouch {src} done: {done} retouched, {refused} refused", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
