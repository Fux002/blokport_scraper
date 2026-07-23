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

# Subfolders under <env>/products/ that hold hosted PRODUCT images + markers. The variant textures live at
# <env>/variations/ -- a SIBLING of products/, NOT under it -- so a products-scoped wipe can never reach them.
_PRODUCT_IMAGE_FOLDERS = ("scraped", "improved", "enhanced", "discarded")


def wipe_all_product_images(client=None, dry_run: bool = False) -> dict:
    """Delete EVERY hosted product image + marker under <env>/products/ (scraped raw, improved treated,
    enhanced/discarded markers) and the url->image manifest(s), so the NEXT scrape re-downloads and
    re-processes from scratch. This is the 'fresh images' half of the pristine (factory) reset; the cheaper
    'clean raw scraped data' keeps these so the manifest reuses them (no GPU/FAL spend). It NEVER touches
    <env>/variations/ (variant textures) or any other prefix/bucket: it lists+deletes ONLY the hardcoded
    <env>/products/<folder>/ prefixes and refuses if any key is somehow outside products/ or under
    variations/. Env-scoped bucket, with the production dev-bucket guard. Idempotent. Returns per-folder
    delete counts."""
    from stone_pipeline.config.settings import IS_PRODUCTION, _DEV_S3_BUCKET

    if IS_PRODUCTION and S3_BUCKET == _DEV_S3_BUCKET:
        raise RuntimeError(
            f"production image wipe targeting the DEV bucket {_DEV_S3_BUCKET} -- refusing (set BLOKPORT_S3_BUCKET)")
    client = client or boto3.client("s3", region_name=S3_REGION)
    products_root = f"{ENV_SEGMENT}/products/"
    variations_root = f"{ENV_SEGMENT}/variations/"        # the protected sibling -- must never appear in a key
    counts: dict[str, int] = {}
    for folder in _PRODUCT_IMAGE_FOLDERS:
        prefix = f"{products_root}{folder}/"
        keys: list[str] = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        for k in keys:                                    # HARD GUARD: refuse the whole wipe on any stray key
            if not k.startswith(prefix) or variations_root in k:
                raise RuntimeError(f"refusing image wipe: key {k!r} is outside {prefix!r} or under variations/")
        counts[folder] = len(keys)
        if not dry_run:
            for i in range(0, len(keys), 1000):           # S3 delete_objects caps at 1000 keys per call
                client.delete_objects(
                    Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
    manifests = [f"{products_root}_manifest.json", f"{products_root}_manifest.backup.json"]
    if not dry_run:                                       # drop the manifest so a re-scrape can't reuse a wiped image
        client.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in manifests]})
    counts["manifest"] = len(manifests)
    return counts


def wipe_source_product_images(source: str, client=None, dry_run: bool = False) -> dict:
    """Delete ONE source's hosted product images + markers under <env>/products/<folder>/<source>/ (scraped
    raw, improved treated, enhanced/discarded markers) and prune that source's entries from the shared
    url->image manifest, so the NEXT scrape of THIS source re-downloads and re-processes it from scratch.
    This is the SCOPED 'expensive' restart -- a per-source hard reset ('Remove data (keep config)' on one
    source) that also drops its images so re-scraping re-spends GPU/FAL; the global twin is
    wipe_all_product_images. It NEVER touches <env>/variations/ (variant textures), another source's images,
    or any other prefix: it lists+deletes ONLY the hardcoded <env>/products/<folder>/<source>/ prefixes and
    refuses if any key is somehow outside them or under variations/. Env-scoped bucket, with the production
    dev-bucket guard. Idempotent. Returns per-folder delete counts + how many manifest entries were pruned."""
    from stone_pipeline.config.settings import IS_PRODUCTION, _DEV_S3_BUCKET

    if not source or "/" in source:                       # a stray '/' would widen the prefix -- refuse
        raise ValueError(f"invalid source name for image wipe: {source!r}")
    if IS_PRODUCTION and S3_BUCKET == _DEV_S3_BUCKET:
        raise RuntimeError(
            f"production image wipe targeting the DEV bucket {_DEV_S3_BUCKET} -- refusing (set BLOKPORT_S3_BUCKET)")
    client = client or boto3.client("s3", region_name=S3_REGION)
    products_root = f"{ENV_SEGMENT}/products/"
    variations_root = f"{ENV_SEGMENT}/variations/"        # the protected sibling -- must never appear in a key
    counts: dict[str, int] = {}
    for folder in _PRODUCT_IMAGE_FOLDERS:
        prefix = f"{products_root}{folder}/{source}/"
        keys: list[str] = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        for k in keys:                                    # HARD GUARD: refuse the whole wipe on any stray key
            if not k.startswith(prefix) or variations_root in k:
                raise RuntimeError(f"refusing image wipe: key {k!r} is outside {prefix!r} or under variations/")
        counts[folder] = len(keys)
        if not dry_run:
            for i in range(0, len(keys), 1000):           # S3 delete_objects caps at 1000 keys per call
                client.delete_objects(
                    Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]]})
    counts["manifest_pruned"] = _prune_manifest_for_source(client, source, products_root, dry_run)
    return counts


def _prune_manifest_for_source(client, source: str, products_root: str, dry_run: bool) -> int:
    """Drop `source`'s entries from the SHARED url->image manifest so a re-scrape re-processes them (a stale
    entry would make the re-scrape reuse a just-deleted image). The manifest is cross-source, so we rewrite
    it minus this source's entries -- never delete the whole file (that is the global wipe's job). Matches an
    entry by its hosted value pointing into any of this source's product-image prefixes. Best-effort."""
    key = f"{products_root}_manifest.json"
    try:
        man = json.loads(client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
    except Exception:
        return 0                                          # no manifest yet -> nothing to prune
    markers = tuple(f"/products/{folder}/{source}/" for folder in _PRODUCT_IMAGE_FOLDERS) + (f"/products/{source}/",)
    kept = {u: v for u, v in man.items() if not any(m in (v or "") for m in markers)}
    pruned = len(man) - len(kept)
    if pruned and not dry_run:
        client.put_object(Bucket=S3_BUCKET, Key=key, Body=json.dumps(kept, sort_keys=True).encode("utf-8"),
                          ContentType="application/json")
    return pruned


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
