"""Reprocess a source's existing scraped/ originals into improved/ (no re-scrape).

For a source already scraped to S3, re-run the de-watermark + enhance + upscale chain
on the raw originals at <env>/products/scraped/<src>/ and write the result to
<env>/products/improved/<src>/<same-hash>.jpg -- straight into the production location
the product sheets link to, replacing any prior version in place (same content key).

Parallelise across tasks with SLICE_OFFSET / SLICE_COUNT (0 count = to the end). SRC
selects the source; WATERMARKED toggles the de-watermark pass. Run on the imageproc
image via the entrypoint:  RUN_MODE=reprocess
"""

from __future__ import annotations

import json
import os
import sys

import boto3

from deploy.enhance_trigger import done_shas   # shared "already enhanced or discarded" delta
from stone_pipeline.config.settings import S3_BUCKET, S3_REGION, ImageProcessingConfig
from stone_pipeline.io import imagestore
from stone_pipeline.io.image_processing import ImageProcessor


def main() -> int:
    src = os.environ.get("SRC", "varsha")
    watermarked = os.environ.get("WATERMARKED", "true").lower() in ("1", "true", "yes")
    classify = os.environ.get("CLASSIFY", "true").lower() in ("1", "true", "yes")
    full = os.environ.get("FULL", "false").lower() in ("1", "true", "yes")  # redo ALL (else only the delta)
    offset = int(os.environ.get("SLICE_OFFSET", "0"))
    count = int(os.environ.get("SLICE_COUNT", "0"))  # 0 = to the end
    src_prefix = imagestore.scraped_prefix(src)      # single source of truth for the S3 layout
    dst_prefix = imagestore.improved_prefix(src)
    client = boto3.client("s3", region_name=S3_REGION)

    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=src_prefix):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    # Slice the STABLE full sorted scraped list -- NEVER a delta-filtered list. Concurrent slices start at
    # different times and would each compute a different "done" set (markers accrue as slices run); slicing
    # a delta that shrinks under you makes identical offsets land on different images -> gaps/overlaps. The
    # full list is timing-independent, so disjoint SLICE_OFFSET windows always cover every image exactly once.
    keys.sort()
    total = len(keys)
    sliced = keys[offset:offset + count] if count else keys[offset:]
    if not sliced:
        print(f"no images in slice (src={src} offset={offset} count={count}); {total} total")
        return 0
    # Incremental is a PER-IMAGE skip inside the loop (never used to slice, so it cannot shift the window):
    # an image already enhanced or discarded is a no-op. FULL=true reprocesses everything (backfill / re-tune).
    skip = set() if full else done_shas(client, src)

    print(f"==> reprocess {src}: slice[{offset}:{offset + len(sliced)}] of {total} full={full} "
          f"watermarked={watermarked} classify={classify} -> s3://{S3_BUCKET}/{dst_prefix}")
    proc = ImageProcessor(ImageProcessingConfig(
        enabled=True, dewatermark=watermarked, classify=classify, write_preview=False))

    enhanced = dw = discarded = skipped = failed = 0
    for i, key in enumerate(sliced):
        name = key.rsplit("/", 1)[-1]          # <sha256>.jpg
        sha = name.rsplit(".", 1)[0]
        if sha in skip:                        # already enhanced or discarded in a prior run -> idempotent no-op
            skipped += 1
            continue
        try:
            data = client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            # 1) classify: a non-stone image (spec sheet / price list / plain logo) is recorded in the
            #    discarded/ pool and NOT enhanced or published. One marker object per image (content-keyed),
            #    so the parallel slices never race a shared file. Stage 7 reads the pool and, when a
            #    variant's every image is discarded, emits the terminal no_publishable_image flag.
            cl = proc.classify(data)
            if cl.ran and not cl.keep:
                marker = json.dumps({"reason": cl.reason, "score": round(cl.p_nonstone, 4),
                                     "classifier": proc.classifier_id}, sort_keys=True).encode("utf-8")
                client.put_object(Bucket=S3_BUCKET, Key=imagestore.discarded_key(src, sha),
                                  Body=marker, ContentType="application/json")
                discarded += 1
                continue
            # 2) a kept image is de-watermarked + enhanced into improved/ (replace-in-place, same key),
            #    then an ENHANCED marker so the incremental delta knows this image is done (produce's raw
            #    re-encode in improved/ is NOT a "done" signal -- only this marker is).
            res = proc.process(data, watermarked=watermarked)
            client.put_object(Bucket=S3_BUCKET, Key=f"{dst_prefix}{name}",
                              Body=res.data, ContentType="image/jpeg")
            client.put_object(Bucket=S3_BUCKET, Key=imagestore.enhanced_key(src, sha),
                              Body=b"", ContentType="text/plain")
            enhanced += 1
            dw += 1 if res.dewatermarked else 0
        except Exception as exc:  # never let one bad image kill the slice
            failed += 1
            print(f"   [{offset + i}] FAILED {name}: {exc}")
        if i % 25 == 0:
            print(f"   [{offset + i}/{offset + len(sliced)}] enhanced={enhanced} dw={dw} "
                  f"discarded={discarded} skipped={skipped} failed={failed}")
    print(f"==> {src} slice done: {enhanced} enhanced, {dw} de-watermarked, "
          f"{discarded} discarded, {skipped} skipped, {failed} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
