"""Standardise a source's scraped photos (RUN_MODE=standardise), on the GPU: reads <env>/products/scraped/<src>/, writes
finished images + the clean slab under <env>/standardise-test/<src>/, never into improved/ (going live is a later step).
Env: SRC; SURVEY=true only classifies every photo and writes the counts; SAMPLE=<n> takes n photos, the same n on every
run (the lowest hashes); SLICE_OFFSET / SLICE_COUNT as in deploy.reprocess_source. A photo already done is skipped, so a
re-run is safe. One bad photo is recorded and never stops the slice.
"""

from __future__ import annotations

import collections
import io
import json
import os
import sys
import time

import boto3
import cv2
import numpy as np

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.io import imagestore
from stone_pipeline.standardise import index, kinds, layout, models, outline, photo


def _encode(rgb, quality=92):
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("could not encode")
    return buf.tobytes()


def _slab_png(slab):
    rgb, alpha = slab
    bgra = np.dstack([cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), (alpha.clip(0, 1) * 255).astype(np.uint8)])
    ok, buf = cv2.imencode(".png", bgra)
    if not ok:
        raise ValueError("could not encode")
    return buf.tobytes()


def _get(client, key):
    return client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()


def _exists(client, prefix):
    return client.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix, MaxKeys=1).get("KeyCount", 0) > 0


def _survey(rgb, m):
    """What this photo would become, without the expensive steps."""
    kind, confidence = kinds.classify(m.clip, rgb)
    if kinds.is_skipped(kind, confidence):
        return f"skip: {kind}"
    quads, _, _, _, reason = outline.find_slabs(m.seg, m.det, rgb)
    if not quads:
        return f"no outline: {reason.split(' (')[0]}"
    return "open book" if len(quads) == 2 else "slab"


def main() -> int:
    src = os.environ.get("SRC", "").strip()
    if not src:
        print("ERROR: SRC is required; aborting")
        return 2
    survey = os.environ.get("SURVEY", "false").lower() in ("1", "true", "yes")
    watermarked = os.environ.get("WATERMARKED", "false").lower() in ("1", "true", "yes")
    extra = os.environ.get("EXTRA_VIEWS", "true").lower() in ("1", "true", "yes")
    sample = int(os.environ.get("SAMPLE", "0"))
    offset, count = int(os.environ.get("SLICE_OFFSET", "0")), int(os.environ.get("SLICE_COUNT", "0"))
    client = boto3.client("s3", region_name=S3_REGION)
    out = f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{src}/"
    photos = index.build(_get(client, f"{ENV_SEGMENT}/{C.SHEET_KEY}").decode("utf-8"))

    keys = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=imagestore.scraped_prefix(src)):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    keys.sort()                                        # the stable order slices rely on (see deploy.reprocess_source)
    known = [k for k in keys if imagestore.sha_from_url(k) in photos]
    if sample:
        known = known[:sample]
    sliced = known[offset:offset + count] if count else known[offset:]
    print(f"==> standardise {src}: {len(sliced)} of {len(known)} known photos ({len(keys)} scraped), survey={survey}", flush=True)
    m = models.load(survey_only=survey)
    tally = collections.Counter()
    spent = []
    for i, key in enumerate(sliced):
        sha = imagestore.sha_from_url(key)
        info = photos[sha]
        started = time.time()
        try:
            if not survey and (_exists(client, f"{out}{sha}.main.jpg") or _exists(client, f"{out}held/{sha}.json")):
                tally["already done"] += 1
                continue
            rgb = photo.decode(_get(client, key))
            if survey:
                tally[_survey(rgb, m) if info["format"] == "slab" else info["format"]] += 1
            else:
                clean = None
                if watermarked:
                    clean = photo.decode(_get(client, f"{imagestore.improved_prefix(src)}{sha}.jpg"))
                res = photo.standardise(rgb, fmt=info["format"], key=info["key"], size=tuple(info["size"]) if info["size"] else None,
                                        clean=clean, watermarked=watermarked, models=m, extra_views=extra)
                if res.held:
                    client.put_object(Bucket=S3_BUCKET, Key=f"{out}held/{sha}.json", ContentType="application/json",
                                      Body=json.dumps({"reason": res.held, "kind": res.kind}, sort_keys=True).encode())
                    tally["held"] += 1
                else:
                    for name, img in res.images.items():
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.{name}.jpg", Body=_encode(img), ContentType="image/jpeg")
                    if res.slab:
                        client.put_object(Bucket=S3_BUCKET, Key=f"{out}{sha}.slab.png", Body=_slab_png(res.slab), ContentType="image/png")
                    tally[res.kind] += 1
        except Exception as exc:                       # one bad photo never stops the slice; recorded and counted
            tally["failed"] += 1
            print(f"   FAILED {sha[:12]}: {type(exc).__name__}: {exc}", flush=True)
        spent.append(time.time() - started)
        if i % 10 == 0:
            print(f"   [{i + 1}/{len(sliced)}] {dict(tally)} median {np.median(spent):.1f}s per photo", flush=True)
    summary = {"source": src, "survey": survey, "counts": dict(tally), "photos": len(sliced),
               "median_seconds": round(float(np.median(spent)), 1) if spent else None}
    name = "survey" if survey else "run"
    client.put_object(Bucket=S3_BUCKET, Key=f"{out}{name}-{offset}.json", Body=json.dumps(summary, sort_keys=True).encode(), ContentType="application/json")
    print(f"==> {src} done: {json.dumps(summary, sort_keys=True)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
