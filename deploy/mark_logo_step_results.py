"""One-off, per environment, at the deploy of the release that retired the logo step (2026-11-02): mark the outdated
open-book results that the retired logo step made, so the photo process never completes them without it.

Why: an outdated open-book result (a side view, no <sha>.slab.png, not tried: deploy.standardise_source.outdated) is
completed by a re-run that publishes its close view and 3D slab without a judge call. A result the retired logo step
made had its logo taken off with a reference copy; its re-run now has no such step, so the completed close view and 3D
slab could show the logo. Marking it tried (noslab/<sha>.json, deploy.standardise_source.tried) keeps its judged views
and choice and gives it no close view and no 3D slab. A per-photo marker, data, never a source setting.

Which results, by data:
- the run tag: STANDARDISE.RUN_TAG only. The logo step never ran on a cleaned copy (the clean tag's input), so a clean
  tag result re-runs from the same input it was judged on.
- the source: one that carried `watermarked` = true in a config.db snapshot taken BEFORE the migration (schema 9 drops
  the column). A snapshot that has no `watermarked` column is refused, loud: pass an earlier S3 version of it.
  Dev's own snapshot is not enough: its varsha flag was switched off in the admin to stop the old enhancer's spend,
  while dev's varsha results were made with it on (1004 of its 1187 are byte copies of prod's run). So for dev pass
  prod's pre-migration snapshot as well; the union is used.

Env (no flags):
  SCRAPER_ENV            the environment whose results are marked (development | production)
  DRY_RUN                required, exactly true or false: true lists and counts, writes nothing
  CONFIG_DB_SNAPSHOTS    optional, comma-separated <bucket>/<key>[@<version id>] of pre-migration config.db snapshots;
                         default: this environment's own snapshot (its latest version)
Idempotent: a result already marked is no longer outdated, so a second run marks nothing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import boto3

from deploy.standardise_source import outdated, round_prefix, tried
from stone_pipeline.config.settings import S3_BUCKET, S3_REGION, STANDARDISE as C
from stone_pipeline.ledger import snapshot

REASON = ("made with the retired logo step (2026-11-02): never completed without it; its judged views and choice "
          "stand, it gets no close view and no 3D slab")


def dry_run() -> bool:
    """DRY_RUN must be stated: true or false, nothing else (a write to S3 is never a default)."""
    value = os.environ.get("DRY_RUN", "").strip().lower()
    if value not in ("true", "false"):
        raise ValueError(f"DRY_RUN must be true or false; got {value!r}")
    return value == "true"


def snapshots() -> list[tuple[str, str, str | None]]:
    """(bucket, key, version id or None) of the config.db snapshots to read."""
    raw = os.environ.get("CONFIG_DB_SNAPSHOTS", "").strip()
    if not raw:
        return [(S3_BUCKET, snapshot.config_key(), None)]
    out = []
    for item in raw.split(","):
        ref, _, version = item.strip().partition("@")
        bucket, _, key = ref.partition("/")
        if not bucket or not key:
            raise ValueError(f"CONFIG_DB_SNAPSHOTS entry {item!r} is not <bucket>/<key>[@<version id>]")
        out.append((bucket, key, version or None))
    return out


def watermarked_sources(path: Path) -> set[str]:
    """The sources a pre-migration config.db marks watermarked. Refuses a migrated one (no `watermarked` column)."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(source)")}
        if "watermarked" not in cols:
            raise ValueError("this config.db snapshot is already migrated (no watermarked column): pass an earlier "
                             "S3 version of it in CONFIG_DB_SNAPSHOTS")
        return {r[0] for r in conn.execute("SELECT source FROM source WHERE watermarked = 1")}


def evidence(client) -> set[str]:
    """The union of the watermarked sources of every snapshot read."""
    found: set[str] = set()
    for bucket, key, version in snapshots():
        extra = {"VersionId": version} if version else {}
        body = client.get_object(Bucket=bucket, Key=key, **extra)["Body"].read()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.db"
            path.write_bytes(body)
            found |= watermarked_sources(path)
    return found


def candidates(client, src) -> list[str]:
    """The shas of src's RUN_TAG results that are outdated open books (the same rule the GPU job applies)."""
    out = round_prefix(C.RUN_TAG, src)
    studio = sorted({o["Key"][len(out):].split(".")[0]
                     for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=out)
                     for o in page.get("Contents", []) if "/" not in o["Key"][len(out):]
                     and o["Key"].endswith(".studio.jpg")})
    return [sha for sha in studio if outdated(client, out, sha)]


def mark(client, sources, write) -> dict[str, list[str]]:
    """{source: shas marked (or, with write off, to mark)}."""
    marked = {}
    for src in sorted(sources):
        shas = candidates(client, src)
        if write:
            for sha in shas:
                tried(client, round_prefix(C.RUN_TAG, src), sha, REASON)
        marked[src] = shas
    return marked


def main() -> int:
    write = not dry_run()
    client = boto3.client("s3", region_name=S3_REGION)
    sources = evidence(client)
    marked = mark(client, sources, write)
    for src, shas in marked.items():
        for sha in shas:
            print(f"   {src} {sha}")
    total = sum(len(s) for s in marked.values())
    print(f"==> {'marked' if write else 'would mark (DRY_RUN)'} {total} outdated open-book results of "
          f"{sorted(sources) or 'no source'} under {C.RUN_TAG} in s3://{S3_BUCKET}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
