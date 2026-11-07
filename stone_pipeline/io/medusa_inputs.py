"""Download the Medusa export inputs from S3 into the local from_medusa/<env>/ dir
BEFORE the run, so the matcher has the current variants export + attributes.

    s3://<staging-bucket>/<env>/scraper/from_medusa/*  ->  from_medusa/<env>/

These export files are MAINTAINED on S3 (you upload a fresh Medusa export there
after each Medusa import). If none are present the matcher simply treats every
variant as new. Auth is the task's IAM role.
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError

from stone_pipeline.config.settings import (ATTRIBUTES_STATUS_REQUIRED, ENV_SEGMENT, S3_BUCKET, SETTINGS,
                                            THIN_COPY_MIN_FRACTION, VARIANTS_EXPORT_STATUS_REQUIRED, category_for_key)
from stone_pipeline.core import logfmt
from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client
from stone_pipeline.reference.medusa_files import read_attributes, read_variants_export

log = logfmt.get_logger("deploy.fetch_inputs")

# A thin/empty S3 copy (Medusa mid-reset, or an export not yet fully seeded) must never overwrite a fuller
# local file, or the run silently starts from the wrong base:
#   - attributes.csv is the committed vocab seed; a thin copy wipes it and NOTHING resolves.
#   - variants_export.csv is the live matching reference; a thin copy makes the matcher treat the whole
#     ~24.7k catalog as new -- mass re-mint / mass gap (the ledger-gap failure mode).
#   - variants_export_base.csv is the committed full seed of truth (base == full).
#   - ports.csv carries the env's Medusa port ids; a thin copy loses port-of-origin resolution for the
#     whole run (every product ships no port), and in prod there is no dev fallback to catch it.
# These keep the local copy unless the S3 copy is at least THIN_COPY_MIN_FRACTION as complete.
_PROTECTED_BASE = {"attributes.csv", "variants_export.csv", "variants_export_base.csv", "ports.csv"}
# A protected file with no local copy is measured against the committed file that lists the same rows: the live
# export is gitignored, so after every roll it is absent while the committed base (the same scraper-owned variants,
# Id-free) is in the image. Without this the guard never ran on a fresh task.
_BASELINE_WHEN_ABSENT = {"variants_export.csv": "variants_export_base.csv"}
# The variant exports list every vendor's variants; the scraper keeps only its own (a Key whose prefix is one of its
# categories). Another vendor's variant is not the scraper's to match, mint or sync, and the ledger refuses it:
# dev 2026-11-01, Blokport's non-stone test products (np-cattle-..., np-diesel-...) aborted the produce.
_VARIANT_EXPORTS = {"variants_export.csv", "variants_export_base.csv"}
# Medusa's downloads, checked by their one reader BEFORE they replace the local copy. settings parses attributes.csv
# at import, so a malformed copy written over the good one would stop every later produce at `import settings`, before
# its own fetch could heal it (every stage reads the variants export the same way); kept out, the next fetch heals the
# file once S3 is fixed.
_READERS = {
    "attributes.csv": lambda path: read_attributes(path, require_status=ATTRIBUTES_STATUS_REQUIRED),
    "variants_export.csv": lambda path: read_variants_export(path, require_status=VARIANTS_EXPORT_STATUS_REQUIRED),
}


def check_readable(name: str, path: Path) -> None:
    """Raise ValueError (naming the file and the problem) when the download `path` of `name` is malformed."""
    reader = _READERS.get(name)
    if reader is not None:
        reader(path)


def own_variants(path: Path) -> int:
    """Rewrite a variant export keeping only the scraper's own variants (and rows without a Key, as before); returns
    how many rows it dropped. A file with nothing to drop is left as it is."""
    with path.open(encoding="utf-8-sig", newline="") as h:
        rows = list(csv.reader(h))
    if not rows or "Key" not in rows[0]:
        return 0
    at = rows[0].index("Key")
    kept = [r for r in rows[1:] if len(r) <= at or not r[at].strip() or category_for_key(r[at])]
    dropped = len(rows) - 1 - len(kept)
    if dropped:
        with path.open("w", encoding="utf-8", newline="") as h:
            csv.writer(h, lineterminator="\n").writerows([rows[0], *kept])
    return dropped


def _line_count(path: Path) -> int:
    try:
        with path.open(encoding="utf-8-sig") as h:
            return sum(1 for _ in h)
    except OSError:
        return 0


def _would_clobber(incoming_lines: int, current_lines: int) -> bool:
    """True when a protected base file already on disk is materially larger than the S3 copy, so the
    download would replace a full seed with a thin one -- keep what we have."""
    return current_lines > 0 and incoming_lines < current_lines * THIN_COPY_MIN_FRACTION


def _refuse_thin(name: str, incoming_lines: int, current_lines: int, basis: str) -> None:
    """A thin S3 copy of `name` was refused. With a local copy, that copy is kept. With none, a copy WITHOUT rows is the
    real cold start (Medusa holds nothing yet): `name` stays absent and the run goes on. A copy WITH rows stops the
    fetch (SystemExit, so the produce stops in every environment): left absent it would look like a cold start, and
    tree_build would publish an EMPTY valid-combinations set that the reconcile gate passes as one."""
    rows, base_rows = max(incoming_lines - 1, 0), max(current_lines - 1, 0)     # less the header line
    if basis == name:
        print(f"   ! kept the local {name} (S3 copy looks thin: {rows} rows vs {base_rows})")
    elif rows == 0:
        print(f"   {name}: no rows on S3 (a cold start); left absent")
    else:
        raise SystemExit(f"refusing {name} from S3: {rows} row(s) against {base_rows} rows in the committed {basis}; "
                         "Medusa's export looks partial (mid-reset?). Re-export it, then run again.")


def _baseline(dest: Path, name: str) -> tuple[int, str]:
    """(line count, the file it was counted on) a protected download must not fall far below: the local copy, else
    the committed file standing in for it (_BASELINE_WHEN_ABSENT), else nothing to protect (0)."""
    for candidate in (name, _BASELINE_WHEN_ABSENT.get(name)):
        if candidate and (dest / candidate).exists():
            return _line_count(dest / candidate), candidate
    return 0, name


def _incoming(target: Path) -> Path:
    """A new, empty temp file beside `target` (the same folder, so replacing it is atomic) for one download: its own
    name per call, so two fetches at once (every hidden-listings page GET fetches attributes.csv, and a produce may run
    beside it) never take each other's file away."""
    handle, name = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.", suffix=".incoming")
    os.close(handle)
    return Path(name)


def fetch_attributes(client=None, target=None) -> bool:
    """Fetch ONLY attributes.csv (the attribute vocab) from S3 into from_medusa/<env>/, honoring the same
    thin-copy floor as the full fetch. The config server calls this on BOOT so the review-decision endpoint
    validates colours/types against Medusa's CURRENT set from container start. Without it a fresh container
    (every deploy reverts the on-disk file to the committed baseline, which lags Medusa's newer colours)
    rejects a just-added colour at mint until the next full produce re-fetches it. An S3 failure (no object, no
    credentials, no endpoint, a transfer whose retries ran out) is logged as an error and keeps the committed copy,
    returning False: a fresh or offline host still validates against a real seed, and one network blip at boot is no
    restart loop. Any other error raises. Returns True iff a fresher copy was
    written. Downloads to a temp then atomically replaces. `target` overrides the destination for tests (defaults
    to the live attributes_csv path)."""
    key = f"{ENV_SEGMENT}/scraper/from_medusa/attributes.csv"
    target = Path(target or SETTINGS.paths.attributes_csv)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _incoming(target)
    try:
        client = client or aws_client("s3", config=BOTOCORE_DEFAULTS)
        client.download_file(S3_BUCKET, key, str(tmp))
        # protected base: never let a thin S3 copy (Medusa mid-reset) replace the fuller committed seed
        if target.exists() and _would_clobber(_line_count(tmp), _line_count(target)):
            return False
        try:
            check_readable("attributes.csv", tmp)
        except ValueError as exc:
            log.error(f"kept the local attributes.csv: the S3 copy is malformed ({exc})")
            return False
        os.replace(tmp, target)
        return True
    except (BotoCoreError, ClientError, Boto3Error) as exc:   # Boto3Error: S3Transfer's RetriesExceededError
        log.error(f"could not fetch s3://{S3_BUCKET}/{key}; keeping the committed attributes.csv: {exc}")
        return False
    finally:
        tmp.unlink(missing_ok=True)


def fetch_all() -> int:
    """Every input of this environment's from_medusa/ fetched from S3 (the produce's first step; deploy/fetch_inputs is
    an operator's manual run of it), each through the thin-copy guard, the reader check and an atomic replace: 0."""
    prefix = f"{ENV_SEGMENT}/scraper/from_medusa/"
    dest = Path(SETTINGS.paths.from_medusa_dir)
    dest.mkdir(parents=True, exist_ok=True)
    client = aws_client("s3", config=BOTOCORE_DEFAULTS)
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
            # one flow for every file: download beside the target, then the thin-copy guard, then the reader check, and
            # only then replace, so a refused or malformed copy never touches the local file
            tmp = _incoming(target)
            try:
                client.download_file(S3_BUCKET, key, str(tmp))
                # filtered BEFORE the thin-copy guard: other vendors' rows must not make a thin stone export look full
                dropped = own_variants(tmp) if name in _VARIANT_EXPORTS else 0
                if name in _PROTECTED_BASE:
                    incoming, (current, basis) = _line_count(tmp), _baseline(dest, name)
                    if _would_clobber(incoming, current):
                        _refuse_thin(name, incoming, current, basis)
                        continue
                check_readable(name, tmp)       # malformed: raise, the produce stops loud with the local file kept
                os.replace(tmp, target)
            finally:
                tmp.unlink(missing_ok=True)
            if dropped:
                print(f"   {name}: dropped {dropped} variants of other vendors (not a scraper category)")
            print(f"   {name}")
            n += 1
    if n == 0:
        print("   (no input files found - matcher will treat everything as new)")
    print(f"==> fetched {n} file(s)")
    return 0

