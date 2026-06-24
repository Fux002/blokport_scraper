"""Reprocess a source's existing scraped/ originals into improved/ (no re-scrape).

For a source already scraped to S3, re-run the de-watermark + enhance + upscale chain
on the raw originals at <env>/products/scraped/<src>/ and write the result to
<env>/products/improved/<src>/<same-hash>.jpg — straight into the production location
the product sheets link to, replacing any prior version in place (same content key).

Parallelise across tasks with SLICE_OFFSET / SLICE_COUNT (0 count = to the end). SRC
selects the source; WATERMARKED toggles the de-watermark pass. Run on the imageproc
image via the entrypoint:  RUN_MODE=reprocess
"""

from __future__ import annotations

import os
import sys

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, ImageProcessingConfig
from stone_pipeline.io.image_processing import ImageProcessor


def main() -> int:
    src = os.environ.get("SRC", "varsha")
    watermarked = os.environ.get("WATERMARKED", "true").lower() in ("1", "true", "yes")
    offset = int(os.environ.get("SLICE_OFFSET", "0"))
    count = int(os.environ.get("SLICE_COUNT", "0"))  # 0 = to the end
    src_prefix = f"{ENV_SEGMENT}/products/scraped/{src}/"
    dst_prefix = f"{ENV_SEGMENT}/products/improved/{src}/"
    client = boto3.client("s3", region_name=S3_REGION)

    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=src_prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    keys.sort()  # stable order so disjoint SLICE_OFFSET windows never overlap across tasks
    sliced = keys[offset:offset + count] if count else keys[offset:]
    if not sliced:
        print(f"no originals in slice (src={src} offset={offset} count={count}); {len(keys)} total")
        return 0

    print(f"==> reprocess {src}: {len(sliced)} of {len(keys)} (offset={offset} "
          f"count={count or 'all'}) watermarked={watermarked} -> s3://{S3_BUCKET}/{dst_prefix}")
    proc = ImageProcessor(ImageProcessingConfig(
        enabled=True, dewatermark=watermarked, write_preview=False))

    done = dw = failed = 0
    for i, key in enumerate(sliced):
        name = key.rsplit("/", 1)[-1]
        try:
            data = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            res = proc.process(data, watermarked=watermarked)
            client.put_object(Bucket=S3_BUCKET, Key=f"{dst_prefix}{name}",
                              Body=res.data, ContentType="image/jpeg")
            done += 1
            dw += 1 if res.dewatermarked else 0
        except Exception as exc:  # never let one bad image kill the slice
            failed += 1
            print(f"   [{offset + i}] FAILED {name}: {exc}")
        if i % 25 == 0:
            print(f"   [{offset + i}/{offset + len(sliced)}] ok={done} dw={dw} failed={failed}")
    print(f"==> {src} slice done: {done}/{len(sliced)} written, {dw} de-watermarked, {failed} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
