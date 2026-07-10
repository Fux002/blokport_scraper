"""Prune staged images no longer referenced by the current product catalog.

Over many scrapes, improved/ and scraped/ accumulate images for products no longer
offered (and extra slab photos beyond the display cap). This deletes every staged image
NOT referenced by the current product CSV, and prunes the matching url->image manifest
entries (so a later re-scrape of a returning URL re-processes it instead of pointing at a
deleted object). Result: S3 holds only the images the shop actually uses.

Safe to run as an occasional one-off (e.g. quarterly). DRY-RUN by default -- it only
reports what it would remove; pass --apply to actually delete.

  python -m deploy.cleanup_images                       # dry-run report
  python -m deploy.cleanup_images --apply               # delete
  python -m deploy.cleanup_images --csv path/to.csv     # override catalog source
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION

_REF = re.compile(r"/improved/([a-z]+)/([0-9a-f]{64})\.jpg")
_MANIFEST_KEY = f"{ENV_SEGMENT}/products/_manifest.json"


def _catalog_text(client, override: str | None) -> str:
    if override:
        return open(override, encoding="utf-8").read()
    # prefer the published catalog on S3; fall back to the local export
    try:
        return client.get_object(
            Bucket=S3_BUCKET, Key=f"{ENV_SEGMENT}/scraper/to_upload/3_products_all.csv"
        )["Body"].read().decode("utf-8")
    except Exception:
        env = ENV_SEGMENT  # dev/prod -> development/production folder
        local = f"to_upload/{'development' if env == 'dev' else 'production'}/3_products_all.csv"
        return open(local, encoding="utf-8").read()


def _referenced(text: str) -> set[tuple[str, str]]:
    refs: set[tuple[str, str]] = set()
    for row in csv.DictReader(io.StringIO(text)):
        for val in row.values():
            for m in _REF.finditer(val or ""):
                refs.add((m.group(1), m.group(2)))
    return refs


def main() -> int:
    apply = "--apply" in sys.argv
    override = None
    if "--csv" in sys.argv:
        override = sys.argv[sys.argv.index("--csv") + 1]
    client = boto3.client("s3", region_name=S3_REGION)

    refs = _referenced(_catalog_text(client, override))
    print(f"catalog references {len(refs)} unique images")
    if not refs:
        print("REFUSING: catalog parsed 0 referenced images — aborting (would delete everything)")
        return 2

    to_delete: list[str] = []
    kept = 0
    for folder in ("improved", "scraped"):
        for src in {s for s, _ in refs}:
            prefix = f"{ENV_SEGMENT}/products/{folder}/{src}/"
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
                for o in page.get("Contents", []):
                    h = o["Key"].rsplit("/", 1)[-1][:-4]
                    if (src, h) in refs:
                        kept += 1
                    else:
                        to_delete.append(o["Key"])
    print(f"keep={kept}  delete={len(to_delete)}  ({'APPLY' if apply else 'dry-run'})")
    for k in to_delete[:8]:
        print("   would delete:", k)

    # manifest entries whose improved target is no longer referenced
    try:
        man = json.loads(client.get_object(Bucket=S3_BUCKET, Key=_MANIFEST_KEY)["Body"].read())
    except Exception:
        man = {}
    stale = {u for u, v in man.items() for m in [_REF.search(v or "")] if m and (m.group(1), m.group(2)) not in refs}
    print(f"manifest entries={len(man)}  stale_to_prune={len(stale)}")

    if apply:
        for i in range(0, len(to_delete), 1000):
            client.delete_objects(Bucket=S3_BUCKET, Delete={
                "Objects": [{"Key": k} for k in to_delete[i:i + 1000]]})
        if stale:
            for u in stale:
                man.pop(u, None)
            client.put_object(Bucket=S3_BUCKET, Key=_MANIFEST_KEY,
                              Body=json.dumps(man, sort_keys=True).encode("utf-8"),
                              ContentType="application/json")
        print(f"DELETED {len(to_delete)} objects, pruned {len(stale)} manifest entries")
    else:
        print("dry-run only; re-run with --apply to delete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
