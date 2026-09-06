"""Wipe hosted product images (the 'fresh images' half of the factory reset).

Image reclamation is driven ONLY by the explicit factory (pristine) reset without keep_images --
never by a diff against the current catalog, and never by a hard/soft reset. A catalog diff cannot
tell a genuinely-abandoned image from one that is in flight (scraped and awaiting enhancement, or
enhanced and awaiting minting): held/pending images are absent from both the catalog and the
manifest by design, and scraped/ is the durable GPU-reprocess input. So the only deletion here is
the whole global wipe, after which a re-scrape re-hosts exactly the images the current scrape
still wants -- nothing in flight is ever lost, because the wipe is always paired with a re-scrape.

  wipe_all_product_images()          the factory reset (stone_pipeline.lifecycle.reset, pristine)

Called by the reset lifecycle only, never on a schedule.
"""
from __future__ import annotations

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION

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
    # Also the RAW-ROOT layout products/<source>/<sha>.jpg (any source staged with NO processor): siblings of
    # the four folders. Sweep everything directly under products/ that is not a reserved folder or the
    # manifest, so "delete EVERY hosted product image" holds. variations/ is a sibling of products/ (never
    # under it), so it can never appear here.
    reserved = tuple(f"{products_root}{f}/" for f in _PRODUCT_IMAGE_FOLDERS)
    manifests = [f"{products_root}_manifest.json", f"{products_root}_manifest.backup.json"]
    root_keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=products_root):
        for o in page.get("Contents", []):
            k = o["Key"]
            if k in manifests or any(k.startswith(r) for r in reserved):
                continue
            if variations_root in k:
                raise RuntimeError(f"refusing image wipe: key {k!r} is under variations/")
            root_keys.append(k)
    counts["raw_root"] = len(root_keys)
    if not dry_run and root_keys:
        for i in range(0, len(root_keys), 1000):
            client.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in root_keys[i:i + 1000]]})
    if not dry_run:                                       # drop the manifest so a re-scrape can't reuse a wiped image
        client.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in manifests]})
    counts["manifest"] = len(manifests)
    return counts
