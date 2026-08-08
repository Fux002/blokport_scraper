"""Wipe hosted product images (the 'fresh images' half of a reset).

Image reclamation is driven ONLY by an explicit, scrape-driven reset -- never by a
diff against the current catalog. A catalog diff cannot tell a genuinely-abandoned
image from one that is in flight (scraped and awaiting enhancement, or enhanced and
awaiting minting): held/pending images are absent from both the catalog and the
manifest by design, and scraped/ is the durable GPU-reprocess input. So the only
deletion here is a whole (per-source or global) wipe, after which a re-produce
re-hosts exactly the images the current scrape still wants -- nothing in flight is
ever lost, because a wipe is always paired with a re-scrape.

  wipe_all_product_images()          the global reset (pristine / factory)
  wipe_source_product_images(src)    the scoped per-source restart

Both are called by the reset lifecycle (stone_pipeline.lifecycle), never on a schedule.
"""
from __future__ import annotations

import json

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION
from stone_pipeline.io.storage import s3_error_is_missing

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
    # Also the RAW-ROOT layout products/<source>/<sha>.jpg (a source staged in s3 mode with NO processor):
    # a sibling of the four subfolders, so it needs its own prefix. The trailing '/' keeps products/<source>/
    # from matching a folder prefix (products/scraped/ etc.). Same hard guard.
    root_prefix = f"{products_root}{source}/"
    root_keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=root_prefix):
        root_keys.extend(o["Key"] for o in page.get("Contents", []))
    for k in root_keys:
        if not k.startswith(root_prefix) or variations_root in k:
            raise RuntimeError(f"refusing image wipe: key {k!r} is outside {root_prefix!r} or under variations/")
    counts["raw_root"] = len(root_keys)
    if not dry_run:
        for i in range(0, len(root_keys), 1000):
            client.delete_objects(Bucket=S3_BUCKET, Delete={"Objects": [{"Key": k} for k in root_keys[i:i + 1000]]})
    counts["manifest_pruned"] = _prune_manifest_for_source(client, source, products_root, dry_run)
    return counts


def _prune_manifest_for_source(client, source: str, products_root: str, dry_run: bool) -> int:
    """Drop `source`'s entries from the SHARED url->image manifest AND its backup, so a re-scrape re-processes
    them (a stale entry would make the re-scrape reuse a just-deleted image; a stale BACKUP would resurrect
    them on a manual restore). The manifest is cross-source, so we rewrite it minus this source's entries --
    never delete the whole file (that is the global wipe's job). Matches an entry by its hosted value pointing
    into any of this source's product-image prefixes. Best-effort. Returns total entries pruned across both."""
    markers = tuple(f"/products/{folder}/{source}/" for folder in _PRODUCT_IMAGE_FOLDERS) + (f"/products/{source}/",)
    pruned = 0
    for name in ("_manifest.json", "_manifest.backup.json"):
        key = f"{products_root}{name}"
        try:
            man = json.loads(client.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
        except Exception as exc:
            if s3_error_is_missing(exc):
                continue                                  # this manifest absent -> nothing to prune
            raise                                         # a real S3 error must surface, not silently skip
        kept = {u: v for u, v in man.items() if not any(m in (v or "") for m in markers)}
        n = len(man) - len(kept)
        pruned += n
        if n and not dry_run:
            client.put_object(Bucket=S3_BUCKET, Key=key, Body=json.dumps(kept, sort_keys=True).encode("utf-8"),
                              ContentType="application/json")
    return pruned
