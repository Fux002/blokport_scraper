"""Add what each published 3D slab is made from to its outline (RUN_MODE=cutout-meta, the ad-hoc task on :core): the
meta-only republish of the 3D slabs publish made before their meta said it. Once, per environment, on the word of the
one who owns that environment.

For each published 3D slab's outline under imagestore.cutout_prefix() with its made-from stamp (a tile's 3D has none and
is left): when the slab it was made from is still the one stamped (its ETag; otherwise publish makes it again, with the
keys, and it is left), its meta gains publish.slab_source's keys (what the run recorded, or the bound source_long_px_max)
and the outline alone is written again, its content type and S3 metadata (the stamp) exactly as they were: the cut-out
(.webp) is never read or written, so publish does not make it again. A meta that already says it is not written (a
second run changes nothing). Nothing is ever guessed: a key that cannot be determined is absent.

Why an existing 3D slab never gets "upscaled": its slab.png cannot prove it. The layout enlarges a flat slab whose width
is below OUT_W * (1 - 2 * MARGIN) = 2048 * 0.93 = 1904.64 px (standardise_settings.py OUT_W, MARGIN; layout._drawn_factor)
TO round(1904.64) = 1905 px across, and draws a slab at least that wide as it is, so a slab exactly 1905 px wide is
drawn 1905 px across either way (an open book's two halves likewise sum to it before the seam trim). Only a run records
which (layout.source_of, in its <sha>.slab.json), and publish carries that into every 3D slab it makes from now on.

A refreshed outline changes its ETag, which is part of the product's standard_version (stages/images.py), so every
product whose outline is refreshed is re-served once. A run therefore names what it refreshes: CUTOUT_META_SCOPE, a
comma-separated list of the 3D slabs' names (their photo's sha, the file name under cutout/<source>/), or "all", which
only production accepts, and only inside the one owner-confirmed re-run. Dev proves it on a handful of named slabs; no
scope at all is refused everywhere.

Prints the counts: added, unchanged, outdated (publish makes it again), tile (no stamp).
"""

from __future__ import annotations

import json
import os
import sys

from botocore.exceptions import ClientError

from stone_pipeline.config.settings import IS_PRODUCTION, S3_BUCKET
from stone_pipeline.io import imagestore
from stone_pipeline.io.storage import PROCESS_RETRIES, aws_client
from stone_pipeline.photos import publish

OUTCOMES = ("added", "unchanged", "outdated", "tile")
ALL = "all"


def scope_from_env():
    """CUTOUT_META_SCOPE (the module doc): the named slabs, a frozenset; None for every slab ("all", production only).
    Exits naming the reason when it is missing, or "all" off production."""
    raw = os.environ.get("CUTOUT_META_SCOPE", "").strip()
    if raw == ALL:
        if not IS_PRODUCTION:
            sys.exit('CUTOUT_META_SCOPE=all is refused off production: name the 3D slabs to refresh')
        return None
    names = frozenset(n.strip() for n in raw.split(",") if n.strip())
    if not names:
        sys.exit('CUTOUT_META_SCOPE is required: the 3D slabs to refresh, comma-separated (or "all" on production)')
    return names


def _etag(client, key):
    """The object's ETag, None when there is none (any other storage error is raised as it is)."""
    try:
        return client.head_object(Bucket=S3_BUCKET, Key=key)["ETag"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def refresh(client, meta_key, scraped_of):
    """One 3D slab's outline given what it is made from (the module doc); its outcome (OUTCOMES). scraped_of(src) =
    {sha: scraped key} of that source."""
    got = client.get_object(Bucket=S3_BUCKET, Key=meta_key)
    stamp = got["Metadata"].get(publish._MADE_FROM)
    if stamp is None:
        return "tile"
    slab_key = stamp.split(" ")[0]
    etag = _etag(client, slab_key)
    if etag is None or stamp != publish.made_from(slab_key, etag):     # gone or made again: publish's to settle
        return "outdated"
    meta = json.loads(got["Body"].read())
    added = meta | publish.slab_source(client, slab_key, scraped_of(meta_key.split("/")[-2]))
    if added == meta:
        return "unchanged"
    client.put_object(Bucket=S3_BUCKET, Key=meta_key, Body=json.dumps(added).encode(), ContentType="application/json",
                      Metadata=got["Metadata"])
    return "added"


def republish(client, scope=None):
    """The published 3D slabs' outlines in `scope` (their names; None: every one) refreshed (refresh), one at a time:
    {outcome: count}."""
    scraped = {}

    def scraped_of(src):
        if src not in scraped:
            scraped[src] = publish.scraped_shas(client, src)
        return scraped[src]
    counts = dict.fromkeys(OUTCOMES, 0)
    metas = sorted(k for k in publish._objects(client, imagestore.cutout_prefix())
                   if k.endswith(".json") and (scope is None or k.rsplit("/", 1)[1][:-len(".json")] in scope))
    for key in metas:
        counts[refresh(client, key, scraped_of)] += 1
    return counts


def main() -> int:
    scope = scope_from_env()
    counts = republish(aws_client("s3", config=PROCESS_RETRIES), scope)
    print("==> 3D slab outlines: " + ", ".join(f"{k} {v}" for k, v in counts.items()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
