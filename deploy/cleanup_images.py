"""Wipe hosted product images (the 'fresh images' half of the factory reset).

Image reclamation is driven ONLY by the explicit factory (pristine) reset without keep_images --
never by a diff against the current catalog, and never by a hard/soft reset. A catalog diff cannot
tell a genuinely-abandoned image from one that is in flight (scraped and awaiting its standardised
photo, or held this run): such images are absent from the catalog by design, and scraped/ is the
photo process's durable input. So the only deletion here is the whole global wipe, after which a
re-scrape re-hosts exactly the images the current scrape still wants, and the next photos run
publishes the chosen standardised photos again from its stored results (<env>/standardise-test/,
never wiped here), so nothing is paid twice.

  wipe_all_product_images()          the factory reset (stone_pipeline.lifecycle.reset, pristine)

Called by the reset lifecycle only, never on a schedule.
"""
from __future__ import annotations

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION

# Subfolders under <env>/products/ that hold hosted PRODUCT images, counted each on their own; everything else under
# products/ (the raw-root layout, the retired enhancer's enhanced/ and discarded/ markers) is swept as "other". The
# variant textures live at <env>/variations/ and the photo process's results at <env>/standardise-test/ -- SIBLINGS of
# products/, NOT under it -- so a products-scoped wipe can never reach them.
_PRODUCT_IMAGE_FOLDERS = ("scraped", "improved", "standard")


def wipe_all_product_images(client=None, dry_run: bool = False) -> dict:
    """Delete EVERY hosted product image under <env>/products/ (the scraped originals, the re-hosted photos, the
    published standardised views and 3D slabs, anything else there) and the url->image manifest(s), so the NEXT scrape
    re-downloads and re-hosts from scratch. This is the 'fresh images' half of the pristine (factory) reset; the cheaper
    'clean raw scraped data' keeps these so the manifest reuses them. It NEVER touches
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
    # Everything else under products/: the RAW-ROOT layout products/<source>/<sha>.jpg (a source staged without
    # processing) and the retired enhancer's markers. Swept so "delete EVERY hosted product image" holds.
    # variations/ is a sibling of products/ (never under it), so it can never appear here.
    reserved = tuple(f"{products_root}{f}/" for f in _PRODUCT_IMAGE_FOLDERS)
    manifests = [f"{products_root}_manifest.json", f"{products_root}_manifest.backup.json"]
    other: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=products_root):
        for o in page.get("Contents", []):
            k = o["Key"]
            if k in manifests or any(k.startswith(r) for r in reserved):
                continue
            if variations_root in k:
                raise RuntimeError(f"refusing image wipe: key {k!r} is under variations/")
            other.append(k)
    counts["other"] = len(other)
    if not dry_run and other:
        for i in range(0, len(other), 1000):
            client.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in other[i:i + 1000]]})
    if not dry_run:                                       # drop the manifest so a re-scrape can't reuse a wiped image
        client.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in manifests]})
    counts["manifest"] = len(manifests)
    return counts
