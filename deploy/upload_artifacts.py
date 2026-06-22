"""Push the pipeline's produced files to S3 so they survive the ephemeral task.

Product images are re-hosted by Stage 7. This mirrors the produced CSVs to the
env's staging bucket under the canonical scraper home `<env>/scraper/`:

    s3://<staging-bucket>/<env>/scraper/to_upload/...   (variants, combinations, products)
    s3://<staging-bucket>/<env>/scraper/review/...      (look-before-upload)

Local source is the per-env folder (to_upload/<env>/) resolved by BLOKPORT_ENV;
the S3 prefix uses ENV_SEGMENT (dev/prod). Auth is the task's IAM role.
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, SETTINGS


def _upload_dir(client, bucket: str, local: Path, prefix: str) -> int:
    if not local.is_dir():
        return 0
    n = 0
    for path in sorted(local.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        key = f"{prefix}/{path.relative_to(local).as_posix()}"
        client.upload_file(str(path), bucket, key)
        print(f"   s3://{bucket}/{key}")
        n += 1
    return n


def main() -> int:
    base = f"{ENV_SEGMENT}/scraper"
    client = boto3.client("s3", region_name=S3_REGION)
    print(f"==> uploading produced files to s3://{S3_BUCKET}/{base}/")
    n = _upload_dir(client, S3_BUCKET, SETTINGS.paths.to_upload_dir, f"{base}/to_upload")
    n += _upload_dir(client, S3_BUCKET, SETTINGS.paths.review_dir, f"{base}/review")
    print(f"==> uploaded {n} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
