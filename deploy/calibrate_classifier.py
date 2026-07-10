"""Calibrate the non-stone image classifier margin against REAL scraped images (no writes).

The classify_margin cannot be guessed: it decides which images get discarded, and a wrong margin either
publishes spec sheets or blanks real slabs. This tool samples a source's scraped/ originals, runs the CLIP
classifier on each, and prints the non-stone probability + winning reason, sorted so the BORDERLINE images
(near the margin) are easiest to eyeball. Read the list, pick a margin that cleanly separates the spec
sheets / logos from the slabs (including slabs-with-sizes, which must stay LOW), then set classify_margin.

Read-only: it never writes the discarded/ pool or improved/. Run on the imageproc/gpu image (needs torch +
the baked CLIP weights):  SRC=<source> SAMPLE=<n> RUN_MODE=... python -m deploy.calibrate_classifier
"""

from __future__ import annotations

import os
import sys

import boto3

from stone_pipeline.config.settings import S3_BUCKET, S3_REGION, ImageProcessingConfig
from stone_pipeline.io import imagestore
from stone_pipeline.io.image_processing import ImageProcessor


def main() -> int:
    src = os.environ.get("SRC", "varsha")
    sample = int(os.environ.get("SAMPLE", "80"))
    stride_env = os.environ.get("STRIDE")  # optional: take every Nth (spread across the source) instead of first N
    src_prefix = imagestore.scraped_prefix(src)
    client = boto3.client("s3", region_name=S3_REGION)

    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=src_prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    keys.sort()
    if not keys:
        print(f"no scraped originals for src={src} at {src_prefix}")
        return 0
    stride = int(stride_env) if stride_env else max(1, len(keys) // sample)
    picked = keys[::stride][:sample]

    proc = ImageProcessor(ImageProcessingConfig(enabled=True, classify=True, dewatermark=False,
                                                write_preview=False))
    if proc._clf is None or not proc._clf.available():
        print("classifier unavailable (need torch + transformers + baked CLIP weights); cannot calibrate")
        return 1

    margin = ImageProcessingConfig().classify_margin
    print(f"==> calibrate {src}: {len(picked)} of {len(keys)} (stride={stride}) "
          f"current margin={margin} classifier={proc.classifier_id}")
    rows = []
    for key in picked:
        name = key.rsplit("/", 1)[-1]
        try:
            data = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            cl = proc.classify(data)
            rows.append((cl.p_nonstone, name, cl.reason))
        except Exception as exc:  # a bad image never stops the sweep
            print(f"   FAILED {name}: {exc}")

    # sort by distance from the margin so the ambiguous cases (the ones that set the threshold) are central
    rows.sort(key=lambda r: r[0])
    would_discard = sum(1 for p, _, _ in rows if p >= margin)
    print("    p_nonstone  would_discard  reason                          image")
    for p, name, reason in rows:
        mark = "DISCARD" if p >= margin else "keep   "
        print(f"    {p:9.3f}   {mark}        {reason[:30]:30}  {name}")
    print(f"==> {would_discard}/{len(rows)} would be discarded at margin={margin}. "
          f"Adjust BLOKPORT_CLIP_MODEL/classify_margin if the split is wrong.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
