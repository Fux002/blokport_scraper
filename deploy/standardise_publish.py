"""Publish the chosen photos (deploy.standardise_select, <CLEAN_TAG>/chosen.json): each listing's one good photo, its views
(showroom, white, side view, close-up) copied from the run that made it into <env>/products/standard/<view>/<src>/<sha>.jpg,
where the image stage links products to them. The folder is made to match the choice exactly: a view no longer chosen is
removed, so a photo the judge or a person turned down never stays live. Server-side copies, nothing downloaded; a copy is
the same bytes, so a re-run is safe. A photo on the reviewed exclude list (EXCLUDE_PATH, one sha per line) is never
published, whatever chose it. DRY_RUN=true prints the plan and changes nothing.
Env: CLEAN_TAG (the tag whose chosen.json is published), DRY_RUN.
"""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.io import imagestore

WORKERS = 32
EXCLUDE_PATH = Path(__file__).resolve().parents[1] / "stone_pipeline" / "config" / "standardise_exclude.txt"
_SHA = re.compile(r"^([0-9a-f]{64})\b")


def read_exclude(text):
    """The shas on the exclude list: the first word of each line, comments and blank lines ignored."""
    return {m.group(1) for line in text.splitlines() if (m := _SHA.match(line.strip()))}


def copies(chosen, made, exclude=frozenset()):
    """[(run key, standard key)] for every view the run made of each chosen photo not on the exclude list, sorted.
    made = the run keys that exist."""
    out = set()
    for c in chosen:
        if c["sha"] in exclude:
            continue
        for view in imagestore.STANDARD_VIEWS:
            key = f"{c['prefix']}{c['sha']}.{view}.jpg"
            if key in made:
                out.add((key, imagestore.standard_key(c["source"], c["sha"], view)))
    return sorted(out)


def stale(published, plan):
    """The published keys the plan no longer has: removed, so the folder matches the choice."""
    return sorted(set(published) - {target for _, target in plan})


def _keys(client, prefix):
    return [o["Key"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])]


def main() -> int:
    tag = os.environ.get("CLEAN_TAG", "").strip()
    if not tag:
        print("ERROR: CLEAN_TAG is required; aborting")
        return 2
    dry = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
    client = boto3.client("s3", region_name=S3_REGION)
    chosen = json.loads(client.get_object(Bucket=S3_BUCKET, Key=f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/chosen.json")["Body"].read())
    made = {k for prefix in sorted({c["prefix"] for c in chosen}) for k in _keys(client, prefix)}
    plan = copies(chosen, made, read_exclude(EXCLUDE_PATH.read_text()))
    published = [k for view in imagestore.STANDARD_VIEWS for k in _keys(client, imagestore.standard_prefix(view))]
    gone = stale(published, plan)
    photos = len({imagestore.sha_from_url(t) for _, t in plan})
    print(f"==> {'DRY RUN: ' if dry else ''}{len(chosen)} listings chosen: {len(plan)} views of {photos} photos to publish, "
          f"{len(gone)} published views no longer chosen", flush=True)
    if dry:
        return 0
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(lambda kv: client.copy_object(Bucket=S3_BUCKET, Key=kv[1], CopySource={"Bucket": S3_BUCKET, "Key": kv[0]},
                                                    ContentType="image/jpeg", MetadataDirective="REPLACE"), plan))
        list(pool.map(lambda k: client.delete_object(Bucket=S3_BUCKET, Key=k), gone))
    print(f"==> published {len(plan)} views, removed {len(gone)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
