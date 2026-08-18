"""De-watermark a SAMPLE of a source's real originals (no re-scrape) for a human to judge.

Reads N watermarked originals already on S3 at <env>/products/scraped/<SRC>/, runs the
de-watermark + enhance chain with THAT source's own prompt, and writes before/after pairs
to <env>/scraper/dewatermark-validation/. Lets you eyeball the result before committing to
the full (slow) set. Run on the imageproc or gpu image:

    SRC=<source> RUN_MODE=validate-dewatermark   (the entrypoint dispatches here)

Needs FAL_KEY (the hosted FAL de-watermarker) plus torch for the ESRGAN enhance.
Tune the count with VALIDATE_N.
"""

from __future__ import annotations

import os
import sys

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, ImageProcessingConfig
from stone_pipeline.io import imagestore
from stone_pipeline.io.image_processing import ImageProcessor


def main() -> int:
    n = int(os.environ.get("VALIDATE_N", "12"))
    src_name = os.environ.get("SRC", "").strip()
    if not src_name:
        print("ERROR: SRC is required (the source to validate); aborting")
        return 2
    src = imagestore.scraped_prefix(src_name)          # single source of truth for the S3 layout
    out = f"{ENV_SEGMENT}/scraper/dewatermark-validation"
    client = boto3.client("s3", region_name=S3_REGION)

    # VALIDATE_KEYS pins specific images (comma-separated object basenames, with or
    # without .jpg) so we can eyeball a known-tricky slab (e.g. the top-banner style);
    # otherwise just take the first N.
    pinned = [k.strip() for k in os.environ.get("VALIDATE_KEYS", "").split(",") if k.strip()]
    if pinned:
        keys = [f"{src}{k if k.endswith('.jpg') else k + '.jpg'}" for k in pinned]
    else:
        keys = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=src):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"])
                if len(keys) >= n:
                    break
            if len(keys) >= n:
                break
    if not keys:
        print(f"no originals at s3://{S3_BUCKET}/{src} — run the pipeline with keep_scraped first")
        return 1

    from stone_pipeline.config.sources import load_source   # the source's own de-watermark prompt (else fallback)
    source_prompt = (load_source(src_name).fal_prompt or "").strip()
    img_kwargs = dict(enabled=True, dewatermark=True, write_preview=False)
    if source_prompt:
        img_kwargs["fal_prompt"] = source_prompt
    proc = ImageProcessor(ImageProcessingConfig(**img_kwargs))
    print(f"==> de-watermarking {len(keys)} {src_name} originals -> s3://{S3_BUCKET}/{out}/")
    hits = 0
    for i, key in enumerate(keys):
        data = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
        client.put_object(Bucket=S3_BUCKET, Key=f"{out}/{i:02d}_before.jpg", Body=data, ContentType="image/jpeg")
        res = proc.process(data, watermarked=True)
        client.put_object(Bucket=S3_BUCKET, Key=f"{out}/{i:02d}_after.jpg", Body=res.data, ContentType="image/jpeg")
        hits += 1 if res.dewatermarked else 0
        print(f"   {i:02d} dewatermarked={res.dewatermarked} upscaled={res.upscaled} <- {key.rsplit('/', 1)[-1]}")
    print(f"==> done: {hits}/{len(keys)} had a watermark detected. Review s3://{S3_BUCKET}/{out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
