"""Delete the S3 staging images for a list of variant Keys.

When you delete variants in Medusa (e.g. the z_variants_to_delete_in_medusa.csv set), their
dev/variations/{Key}.png objects should go too. This reads a Keys file and removes each
<env>/variations/{Key}.png from the staging bucket.

    python -m stone_pipeline.delete_variant_images                 # DRY RUN on the z-delete list
    python -m stone_pipeline.delete_variant_images --apply         # actually delete
    python -m stone_pipeline.delete_variant_images other.csv --apply

Dry-run by default (lists what it would delete); --apply performs the deletes. boto3 is lazy, so
this runs where AWS credentials are configured.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("delete_images")


def _keys_from(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="") as h:
        return [(r.get("Key") or "").strip() for r in csv.DictReader(h) if (r.get("Key") or "").strip()]


def _deleter(apply: bool):
    """Return a callable key -> ('deleted'|'missing'|'would-delete'), or None if S3 is unreachable."""
    s3 = SETTINGS.s3
    if not apply:
        return lambda full_key: "would-delete"
    try:
        import boto3
    except ImportError:
        return None
    client = boto3.Session(profile_name=s3.credentials_profile, region_name=s3.region).client("s3")

    def delete(full_key: str) -> str:
        try:
            client.head_object(Bucket=s3.bucket, Key=full_key)
        except Exception:
            return "missing"
        client.delete_object(Bucket=s3.bucket, Key=full_key)
        return "deleted"
    return delete


def run(keys: list[str], apply: bool = False) -> dict[str, int]:
    delete = _deleter(apply)
    prefix = f"{ENV_SEGMENT}/variations/"
    counts = {"deleted": 0, "missing": 0, "would-delete": 0}
    if delete is None:
        log.warning("S3 not reachable (no boto3 / credentials); nothing deleted")
        return counts
    for key in keys:
        outcome = delete(f"{prefix}{key}.png")
        counts[outcome] = counts.get(outcome, 0) + 1
    log.info("variant image deletion", extra={"extra_fields": {**counts, "apply": apply}})
    return counts


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    apply = "--apply" in argv
    files = [a for a in argv if not a.startswith("-")]
    path = Path(files[0]) if files else SETTINGS.paths.review_dir / "z_variants_to_delete_in_medusa.csv"
    if not path.exists():
        print(f"no key file at {path}")
        return 1
    keys = _keys_from(path)
    counts = run(keys, apply=apply)
    verb = "deleted" if apply else "would delete (dry-run; pass --apply)"
    print(f"{verb}: {counts.get('deleted', 0) + counts.get('would-delete', 0)} images "
          f"({counts.get('missing', 0)} already absent) from {len(keys)} keys in {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
