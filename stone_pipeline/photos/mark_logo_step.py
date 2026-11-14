"""One-off, per environment, at the deploy of the release that retired the logo step: mark every result the retired
logo step made, so the photo process never adds to it without that step.

Why: later steps make views of a stored result again from a re-run of its photo: completing an outdated open book (its
close view and 3D slab, unjudged: stone_pipeline.photos.source.rework), the close-only run (its close-up again, every
close-up rule, unjudged: stone_pipeline.photos.close), and the outline recheck (a whole slab drawn differently is redrawn
as a candidate the judge scores before it replaces the result: rework, stone_pipeline.photos.select.settle). A result the
retired logo step made had its logo taken off with a reference copy; a re-run now has no such step, so what it makes
starts from the photo with its logo, not the logo-free copy the result was judged as. The marker (logo-step/<sha>.json,
stone_pipeline.photos.source.logo_step_key) stops all three, whatever the rule: the result keeps its judged views, its
choice and its close.jpg as they are. A per-photo marker, data, never a source setting.

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
Idempotent: a result already marked is left as it is, so a second run marks nothing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


from stone_pipeline.photos.source import LOGO_STEP, logo_step_key, round_prefix
from stone_pipeline.config.settings import S3_BUCKET
from stone_pipeline.config.standardise_settings import STANDARDISE as C
from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client
from stone_pipeline.ledger import snapshot

REASON = ("made with the retired logo step (2026-11-02): never completed and its close-up never made again without it; "
          "its judged views, choice and close-up stand")


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
    """The shas of src's RUN_TAG results (each with its <sha>.main.jpg) not marked yet."""
    out = round_prefix(C.RUN_TAG, src)
    keys = {o["Key"][len(out):] for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=out)
            for o in page.get("Contents", [])}
    marked = {k[len(LOGO_STEP):].split(".")[0] for k in keys if k.startswith(LOGO_STEP)}
    return sorted({k.split(".")[0] for k in keys if "/" not in k and k.endswith(".main.jpg")} - marked)


def mark(client, sources, write) -> dict[str, list[str]]:
    """{source: shas marked (or, with write off, to mark)}."""
    marked = {}
    for src in sorted(sources):
        shas = candidates(client, src)
        if write:
            out = round_prefix(C.RUN_TAG, src)
            for sha in shas:
                client.put_object(Bucket=S3_BUCKET, Key=logo_step_key(out, sha), ContentType="application/json",
                                  Body=json.dumps({"reason": REASON}, sort_keys=True).encode())
        marked[src] = shas
    return marked


def main() -> int:
    write = not dry_run()
    client = aws_client("s3", config=BOTOCORE_DEFAULTS)
    sources = evidence(client)
    marked = mark(client, sources, write)
    for src, shas in marked.items():
        for sha in shas:
            print(f"   {src} {sha}")
    total = sum(len(s) for s in marked.values())
    print(f"==> {'marked' if write else 'would mark (DRY_RUN)'} {total} results of "
          f"{sorted(sources) or 'no source'} under {C.RUN_TAG} in s3://{S3_BUCKET}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
