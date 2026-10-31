"""Publish a finished standardise run: copy each photo's views (showroom, white, side view, close-up) from the run's
folder into <env>/products/standard/<view>/<src>/<sha>.jpg, where the image stage links products to them. Server-side
copies, so nothing is downloaded; a copy is the same bytes, so a re-run is safe. A held photo has no views and
publishes nothing: its product keeps the images it had.
Env: RUN_TAG (the run to publish), SRC (space-separated sources).
"""

from __future__ import annotations

import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3

from stone_pipeline.config.settings import S3_BUCKET, S3_REGION
from stone_pipeline.io import imagestore
from deploy.standardise_source import run_prefix

_VIEW = re.compile(r"/([0-9a-f]{64})\.(" + "|".join(imagestore.STANDARD_VIEWS) + r")\.jpg$")
WORKERS = 32


def copies(src, prefix, keys):
    """[(run key, standard key)] for every finished view of the run's photos, sorted."""
    out = []
    for key in keys:
        m = _VIEW.search(key)
        if m and key.startswith(prefix) and "/" not in key[len(prefix):]:
            out.append((key, imagestore.standard_key(src, m.group(1), m.group(2))))
    return sorted(out)


def main() -> int:
    if not os.environ.get("RUN_TAG", "").strip() or not os.environ.get("SRC", "").strip():
        print("ERROR: RUN_TAG and SRC are required; aborting")
        return 2
    client = boto3.client("s3", region_name=S3_REGION)
    total = 0
    for src in os.environ["SRC"].split():
        prefix = run_prefix(src)
        keys = [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
                for o in page.get("Contents", [])]
        todo = copies(src, prefix, keys)
        with ThreadPoolExecutor(WORKERS) as pool:
            list(pool.map(lambda kv: client.copy_object(Bucket=S3_BUCKET, Key=kv[1], CopySource={"Bucket": S3_BUCKET, "Key": kv[0]},
                                                        ContentType="image/jpeg", MetadataDirective="REPLACE"), todo))
        photos = len({imagestore.sha_from_url(t) for _, t in todo})
        print(f"==> {src}: published {len(todo)} views of {photos} photos from {prefix}", flush=True)
        total += len(todo)
    print(f"==> published {total} views", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
