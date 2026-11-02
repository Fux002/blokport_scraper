"""Publish the chosen photos (deploy.standardise_select: one record per listing under <CLEAN_TAG>/chosen/, together the
complete choice whatever runs made them): each listing's one good photo, its views
(showroom, white, side view, close-up) copied from the run that made it into <env>/products/standard/<view>/<src>/<sha>.jpg,
where the image stage links products to them. A chosen photo of a whole single slab (its run saved <sha>.slab.png) also
gets its 3D slab, made here from that slab (stone_pipeline.standardise.cutout) into
<env>/products/standard/cutout/<src>/<sha>.webp + .json; only a missing one or one older than its slab is made again.
The folder is made to match the choice exactly: a view or 3D slab no longer chosen is removed, so a photo the judge or a
person turned down never stays live. Views are server-side copies of the same bytes and a cut-out is deterministic, so a
re-run is safe. A photo on the reviewed exclude list (EXCLUDE_PATH, one sha per line) is never published, whatever chose
it. DRY_RUN=true prints the plan and changes nothing.
No choice records at all is refused: the folder would be emptied.
Env: CLEAN_TAG (the tag whose choice records are published), DRY_RUN.
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
from stone_pipeline.standardise import cutout

WORKERS = 32
EXCLUDE_PATH = Path(__file__).resolve().parents[1] / "stone_pipeline" / "config" / "standardise_exclude.txt"
_SHA = re.compile(r"^([0-9a-f]{64})\b")


def chosen_prefix(root):
    """Where the choice of each listing is kept: <clean tag root>chosen/<listing>.json, one record per listing with a good
    photo."""
    return f"{root}chosen/"


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


def slabs(chosen, made, exclude=frozenset()):
    """{(source, sha): its run's slab key} for every chosen photo not on the exclude list whose run saved a whole single
    slab: the photos that get a 3D slab."""
    return {(c["source"], c["sha"]): key for c in chosen
            if c["sha"] not in exclude and (key := f"{c['prefix']}{c['sha']}.slab.png") in made}


def cutout_keys(slab):
    """The two published files of one photo's 3D slab, the outline last (written last: it means the pair is complete)."""
    return [imagestore.cutout_key(*slab, "webp"), imagestore.cutout_key(*slab, "json")]


def outdated(todo, made, published):
    """The (source, sha) of `todo` whose 3D slab is missing, half written or older than its run's slab (a later run made
    the photo again), sorted. made, published = {key: last modified}."""
    return sorted(s for s, slab in todo.items()
                  if any(k not in published or published[k] < made[slab] for k in cutout_keys(s)))


def stale(published, keep):
    """The published keys the choice no longer has: removed, so the folder matches the choice."""
    return sorted(set(published) - set(keep))


def _objects(client, prefix):
    """{key: last modified} under a prefix."""
    return {o["Key"]: o["LastModified"] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])}


def _publish_cutout(client, slab, slab_key):
    """Make and write one photo's 3D slab. A slab that is not one (ValueError) is reported and that photo's 3D slab, an
    older one included, removed: that product simply has no 3D, the run goes on."""
    try:
        files = cutout.make(client.get_object(Bucket=S3_BUCKET, Key=slab_key)["Body"].read())
    except ValueError as exc:
        print(f"   NO 3D {slab[0]}/{slab[1][:12]}: {exc}", flush=True)
        for key in cutout_keys(slab):
            client.delete_object(Bucket=S3_BUCKET, Key=key)
        return False
    for key, body, kind in zip(cutout_keys(slab), files, ("image/webp", "application/json")):
        client.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=kind)
    return True


def main() -> int:
    tag = os.environ.get("CLEAN_TAG", "").strip()
    if not tag:
        print("ERROR: CLEAN_TAG is required; aborting")
        return 2
    dry = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
    client = boto3.client("s3", region_name=S3_REGION)
    records = list(_objects(client, chosen_prefix(f"{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/")))
    with ThreadPoolExecutor(WORKERS) as pool:
        chosen = list(pool.map(lambda k: json.loads(client.get_object(Bucket=S3_BUCKET, Key=k)["Body"].read()), records))
    if not chosen:
        print(f"ERROR: no choice records under {chosen_prefix(f'{ENV_SEGMENT}/{C.TEST_PREFIX}/{tag}/')}: nothing published or removed")
        return 2
    made = {k: t for prefix in sorted({c["prefix"] for c in chosen}) for k, t in _objects(client, prefix).items()}
    exclude = read_exclude(EXCLUDE_PATH.read_text())
    plan, todo = copies(chosen, made, exclude), slabs(chosen, made, exclude)
    published = {k: t for prefix in [*map(imagestore.standard_prefix, imagestore.STANDARD_VIEWS), imagestore.cutout_prefix()]
                 for k, t in _objects(client, prefix).items()}
    gone = stale(published, [t for _, t in plan] + [k for s in todo for k in cutout_keys(s)])
    remake = outdated(todo, made, published)
    photos = len({imagestore.sha_from_url(t) for _, t in plan})
    print(f"==> {'DRY RUN: ' if dry else ''}{len(chosen)} listings chosen: {len(plan)} views of {photos} photos to publish, "
          f"{len(todo)} 3D slabs ({len(remake)} to make), {len(gone)} published files no longer chosen", flush=True)
    if dry:
        return 0
    with ThreadPoolExecutor(WORKERS) as pool:
        list(pool.map(lambda kv: client.copy_object(Bucket=S3_BUCKET, Key=kv[1], CopySource={"Bucket": S3_BUCKET, "Key": kv[0]},
                                                    ContentType="image/jpeg", MetadataDirective="REPLACE"), plan))
        made_3d = sum(pool.map(lambda s: _publish_cutout(client, s, todo[s]), remake))
        list(pool.map(lambda k: client.delete_object(Bucket=S3_BUCKET, Key=k), gone))
    print(f"==> published {len(plan)} views, made {made_3d} of {len(remake)} 3D slabs, removed {len(gone)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
