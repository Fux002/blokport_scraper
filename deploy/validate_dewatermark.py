"""De-watermark a SAMPLE of real varsha originals (no re-scrape) for a human to judge.

Reads N watermarked originals already on S3 at <env>/products/scraped/varsha/, runs
the de-watermark + enhance chain, and writes before/after pairs to
<env>/scraper/dewatermark-validation/. Lets you eyeball the result — especially the
centered "Varsha Stone" mark, the hardest case for faithful inpainting — before
committing to the full (slow) set. Run on the imageproc image:

    RUN_MODE=validate-dewatermark   (the entrypoint dispatches here)

Needs torch + Florence-2 + LaMa (the imageproc image). Tune the count with VALIDATE_N.
"""

from __future__ import annotations

import os
import sys

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, ImageProcessingConfig
from stone_pipeline.io.image_processing import ImageProcessor


def main() -> int:
    n = int(os.environ.get("VALIDATE_N", "12"))
    src = f"{ENV_SEGMENT}/products/scraped/varsha/"
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

    # Detection prompt is overridable via VALIDATE_PROMPT so we can try "logo" / "text"
    # / "watermark" on the sample without rebuilding the image each time.
    prompt = os.environ.get("VALIDATE_PROMPT", "logo,watermark")
    proc = ImageProcessor(ImageProcessingConfig(
        enabled=True, dewatermark=True, write_preview=False, dewatermark_prompt=prompt))
    print(f"==> de-watermarking {len(keys)} varsha originals (prompt={prompt!r}) -> s3://{S3_BUCKET}/{out}/")
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
