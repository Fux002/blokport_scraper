"""Download the Medusa export inputs from S3 into the local from_medusa/<env>/ dir
BEFORE the run, so the matcher has the current variants export + attributes.

    s3://<staging-bucket>/<env>/scraper/from_medusa/*  ->  from_medusa/<env>/

These export files are MAINTAINED on S3 (you upload a fresh Medusa export there
after each Medusa import). If none are present the matcher simply treats every
variant as new. Auth is the task's IAM role.
"""

from __future__ import annotations

import sys
from pathlib import Path

import boto3

from stone_pipeline.config.settings import ENV_SEGMENT, S3_BUCKET, S3_REGION, SETTINGS

# A thin/empty S3 copy (Medusa mid-reset, or an export not yet fully seeded) must never overwrite a fuller
# local file, or the run silently starts from the wrong base:
#   - attributes.csv is the committed vocab seed; a thin copy wipes it and NOTHING resolves.
#   - variants_export.csv is the live matching reference; a thin copy makes the matcher treat the whole
#     ~24.7k catalog as new -- mass re-mint / mass gap (the ledger-gap failure mode).
#   - variants_export_base.csv is the committed full seed of truth (base == full).
# These keep the local copy unless the S3 copy is at least half as complete (_MIN_KEEP_FRACTION).
_PROTECTED_BASE = {"attributes.csv", "variants_export.csv", "variants_export_base.csv"}
_MIN_KEEP_FRACTION = 0.5


def _line_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8-sig") as h:
            return sum(1 for _ in h)
    except OSError:
        return 0


def _would_clobber(incoming_lines: int, current_lines: int) -> bool:
    """True when a protected base file already on disk is materially larger than the S3 copy, so the
    download would replace a full seed with a thin one -- keep what we have."""
    return current_lines > 0 and incoming_lines < current_lines * _MIN_KEEP_FRACTION


def fetch_attributes(client=None, target=None) -> bool:
    """Fetch ONLY attributes.csv (the attribute vocab) from S3 into from_medusa/<env>/, honoring the same
    thin-copy floor as the full fetch. The config server calls this on BOOT so the review-decision endpoint
    validates colours/types against Medusa's CURRENT set from container start. Without it a fresh container
    (every deploy reverts the on-disk file to the committed baseline, which lags Medusa's newer colours)
    rejects a just-added colour at mint until the next full produce re-fetches it. Best-effort: returns False
    and keeps the committed copy on ANY error (a fresh/offline host still validates against a real seed).
    Returns True iff a fresher copy was written. Downloads to a temp then atomically replaces. `target`
    overrides the destination for tests (defaults to the live attributes_csv path)."""
    key = f"{ENV_SEGMENT}/scraper/from_medusa/attributes.csv"
    target = Path(target or SETTINGS.paths.attributes_csv)
    tmp = target.with_suffix(target.suffix + ".incoming")
    try:
        client = client or boto3.client("s3", region_name=S3_REGION)
        target.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(S3_BUCKET, key, str(tmp))
        # protected base: never let a thin S3 copy (Medusa mid-reset) replace the fuller committed seed
        if target.exists() and _would_clobber(_line_count(tmp), _line_count(target)):
            return False
        tmp.replace(target)
        return True
    except Exception:
        return False
    finally:
        if tmp.exists():
            tmp.unlink()


def main() -> int:
    prefix = f"{ENV_SEGMENT}/scraper/from_medusa/"
    dest = Path(SETTINGS.paths.from_medusa_dir)
    dest.mkdir(parents=True, exist_ok=True)
    client = boto3.client("s3", region_name=S3_REGION)
    print(f"==> fetching inputs from s3://{S3_BUCKET}/{prefix} -> {dest}")
    n = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = key[len(prefix):]
            if not name:  # the prefix "folder" placeholder
                continue
            target = dest / name
            # guard against a key with '..' segments resolving outside dest (path traversal)
            if not target.resolve().is_relative_to(dest.resolve()):
                print(f"   ! skipped (key escapes {dest.name}/): {key}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if name in _PROTECTED_BASE and target.exists():
                tmp = target.with_suffix(target.suffix + ".incoming")
                client.download_file(S3_BUCKET, key, str(tmp))
                incoming, current = _line_count(tmp), _line_count(target)
                if _would_clobber(incoming, current):
                    print(f"   ! kept committed {name} (S3 copy looks thin: {incoming} vs {current} lines)")
                    tmp.unlink()
                    continue
                tmp.replace(target)
            else:
                client.download_file(S3_BUCKET, key, str(target))
            print(f"   {name}")
            n += 1
    if n == 0:
        print("   (no input files found — matcher will treat everything as new)")
    print(f"==> fetched {n} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
