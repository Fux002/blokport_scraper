"""Download the Medusa export inputs from S3 into the local from_medusa/<env>/ dir
BEFORE the run, so the matcher has the current variants export + attributes.

    s3://<staging-bucket>/<env>/scraper/from_medusa/*  ->  from_medusa/<env>/

These export files are MAINTAINED on S3 (you upload a fresh Medusa export there
after each Medusa import). If none are present the matcher simply treats every
variant as new. Auth is the task's IAM role.
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, SETTINGS


def main() -> int:
    prefix = f"{ENV_SEGMENT}/scraper/from_medusa/"
    dest = Path(SETTINGS.paths.from_medusa_dir)
    dest.mkdir(parents=True, exist_ok=True)
    client = boto3.client("s3", region_name=S3_REGION)
    print(f"==> fetching inputs from s3://{S3_BUCKET}/{prefix} -> {dest}")
    n = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = key[len(prefix):]
            if not name:  # the prefix "folder" placeholder
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(S3_BUCKET, key, str(target))
            print(f"   {name}")
            n += 1
    if n == 0:
        print("   (no input files found — matcher will treat everything as new)")
    print(f"==> fetched {n} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
