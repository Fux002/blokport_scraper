"""Add what each published 3D slab is made from to its outline (RUN_MODE=cutout-meta, the ad-hoc task on :core): the
meta-only republish of the 3D slabs publish made before their meta said it (Mas, 2026-11-10). Once, per environment, on
the word of the one who owns it (dev: Mas; prod: the owner).

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

Prints the counts: added, unchanged, outdated (publish makes it again), tile (no stamp).
"""

from __future__ import annotations

import json
import sys

from botocore.exceptions import ClientError

from stone_pipeline.config.settings import S3_BUCKET
from stone_pipeline.io import imagestore
from stone_pipeline.io.storage import PROCESS_RETRIES, aws_client
from stone_pipeline.photos import publish

OUTCOMES = ("added", "unchanged", "outdated", "tile")


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


def republish(client):
    """Every published 3D slab's outline refreshed (refresh), one at a time: {outcome: count}."""
    scraped = {}

    def scraped_of(src):
        if src not in scraped:
            scraped[src] = publish.scraped_shas(client, src)
        return scraped[src]
    counts = dict.fromkeys(OUTCOMES, 0)
    metas = sorted(k for k in publish._objects(client, imagestore.cutout_prefix()) if k.endswith(".json"))
    for key in metas:
        counts[refresh(client, key, scraped_of)] += 1
    return counts


def main() -> int:
    counts = republish(aws_client("s3", config=PROCESS_RETRIES))
    print("==> 3D slab outlines: " + ", ".join(f"{k} {v}" for k, v in counts.items()), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
