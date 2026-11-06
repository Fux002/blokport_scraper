"""Publish the pipeline's deliverables to S3 under a content-hash manifest.

The produced CSVs are mirrored to the env's staging bucket under the canonical scraper home `<env>/scraper/`:

    s3://<staging-bucket>/<env>/scraper/to_upload/...      (variants, combinations, products)
    s3://<staging-bucket>/<env>/scraper/to_upload/manifest.json
    s3://<staging-bucket>/<env>/scraper/review/...         (look-before-upload)

THE CONTRACT (Blokport's import reads it):
  * `manifest.json` lists every deliverable with the SHA-256 of the exact bytes of the S3 object, its data row
    count (CSV records, header and empty lines excluded), its size, and the S3 VersionId of the object holding
    those bytes. Blokport keys its "combinations changed?" guard on the full set's sha256, hashes the stream it
    imports and fails loud on a mismatch.
  * The deliverables keep their fixed names, so a reader of `<prefix>/<name>` is unchanged. A fixed name is
    overwritten in place while a publish runs, so its bytes can be newer than the manifest on S3 (the reader's
    hash then mismatches: retry). The VersionId closes that window for a reader that wants it: a GET with
    `versionId` reads exactly the bytes the manifest hashes, whatever a publish is doing. S3 verifies each upload
    against the manifest's sha256 (ChecksumSHA256), so the named version holds those bytes. The bucket must be
    versioned; an upload without a VersionId fails loud.
  * A deliverable is uploaded ONLY when its hash differs from the manifest currently on S3 (or that entry names no
    version yet), so unchanged content keeps its object and timestamp; the manifest is written LAST, on every
    publish, with a fresh run_id / produced_at even when every hash is unchanged (a "nothing changed" produce is a
    clean no-op on the guard, never a block). A manifest therefore never names content that is not already there.
  * Files in to_upload/ that are not deliverables (settings.UPLOAD_SIDE_FILES: the operator checklist, the photo
    process's list, the texture fingerprints) are uploaded beside them on every publish but are not in the
    manifest: no importer reads them through it.
  * The manifest on S3 is the ONLY record of what is published; no local state.

Local source is the per-env folder (to_upload/<env>/) resolved by BLOKPORT_ENV; the S3 prefix uses ENV_SEGMENT
(dev/prod). Auth is the task's IAM role. An S3 failure raises: the runner logs it loudly and keeps the produce.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from stone_pipeline.config.settings import ENV_NAME, ENV_SEGMENT, S3_BUCKET, S3_REGION, SETTINGS, UPLOAD_SIDE_FILES

MANIFEST_CONTRACT = "v1"
MANIFEST_NAME = "manifest.json"
_SKIP_NAMES = {".DS_Store", MANIFEST_NAME}
_CHUNK = 1 << 20


def file_entry(path: Path) -> dict:
    """{sha256, rows, bytes} of one deliverable: sha256 over the exact bytes (what an importer hashing the S3
    stream sees), rows = CSV records header excluded (csv-parsed, so a quoted newline is not a row; a truly empty
    line, which csv.reader yields as [], is not a record, while one of empty fields like ',,,' is); a non-CSV file
    carries no rows."""
    digest = hashlib.sha256()
    with path.open("rb") as h:
        for chunk in iter(lambda: h.read(_CHUNK), b""):
            digest.update(chunk)
    entry = {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as h:
            entry["rows"] = max(sum(1 for record in csv.reader(h) if record) - 1, 0)
    return entry


def published_manifest(client, bucket: str, key: str) -> dict:
    """The manifest currently on S3, or an empty one when none has been published yet. Any other S3 error
    raises: publishing against an unknown state would re-upload everything or, worse, skip a changed file."""
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return {}
        raise
    return json.loads(body)


def _put_version(client, bucket: str, key: str, path: Path, sha256: str) -> str:
    """Upload one deliverable and return the VersionId of the object written. S3 checks the bytes it stores against
    `sha256` (the manifest's), so the version the manifest names is exactly the hashed content."""
    with path.open("rb") as body:
        response = client.put_object(Bucket=bucket, Key=key, Body=body,
                                     ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode("ascii"))
    version = response.get("VersionId")
    if not version or version == "null":
        raise RuntimeError(f"s3://{bucket}/{key} was written without a version id: the manifest names each "
                           "deliverable's version, so the bucket must have versioning enabled")
    return version


def publish_deliverables(client, bucket: str, local: Path, prefix: str, run_id: str | None) -> dict:
    """Upload the changed deliverables under `local` and every side file, then write the manifest. Returns
    {"uploaded": [...], "unchanged": [...], "side": [...], "manifest": <the manifest written>}."""
    manifest_key = f"{prefix}/{MANIFEST_NAME}"
    previous = published_manifest(client, bucket, manifest_key).get("files", {})
    files: dict[str, dict] = {}
    uploaded: list[str] = []
    unchanged: list[str] = []
    side: list[str] = []
    for path in sorted(p for p in local.rglob("*") if p.is_file() and p.name not in _SKIP_NAMES):
        name = path.relative_to(local).as_posix()
        if name in UPLOAD_SIDE_FILES:
            client.upload_file(str(path), bucket, f"{prefix}/{name}")
            side.append(name)
            continue
        entry = file_entry(path)
        prior = previous.get(name, {})
        if prior.get("sha256") == entry["sha256"] and prior.get("version_id"):
            entry["version_id"] = prior["version_id"]
            unchanged.append(name)
        else:
            entry["version_id"] = _put_version(client, bucket, f"{prefix}/{name}", path, entry["sha256"])
            uploaded.append(name)
        files[name] = entry
    manifest = {"contract": MANIFEST_CONTRACT, "env": ENV_NAME, "run_id": run_id,
                "produced_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "files": files}
    client.put_object(Bucket=bucket, Key=manifest_key, ContentType="application/json",
                      Body=json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
    return {"uploaded": uploaded, "unchanged": unchanged, "side": side, "manifest": manifest}


def _upload_dir(client, bucket: str, local: Path, prefix: str) -> int:
    """Mirror a directory as-is (the review files: small, no consumer contract)."""
    if not local.is_dir():
        return 0
    n = 0
    for path in sorted(local.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        client.upload_file(str(path), bucket, f"{prefix}/{path.relative_to(local).as_posix()}")
        n += 1
    return n


def main(run_id: str | None = None) -> int:
    base = f"{ENV_SEGMENT}/scraper"
    client = boto3.client("s3", region_name=S3_REGION)
    print(f"==> publishing deliverables to s3://{S3_BUCKET}/{base}/to_upload/")
    if not SETTINGS.paths.to_upload_dir.is_dir():
        print("   nothing to publish (no to_upload dir)")
        return 0
    result = publish_deliverables(client, S3_BUCKET, SETTINGS.paths.to_upload_dir, f"{base}/to_upload", run_id)
    for name in result["uploaded"]:
        print(f"   uploaded  {name}")
    for name in result["unchanged"]:
        print(f"   unchanged {name}")
    for name in result["side"]:
        print(f"   side file {name} (not in the manifest)")
    print(f"   manifest  {MANIFEST_NAME} ({len(result['manifest']['files'])} file(s), run {run_id or '-'})")
    n = _upload_dir(client, S3_BUCKET, SETTINGS.paths.review_dir, f"{base}/review")
    print(f"==> uploaded {len(result['uploaded'])} changed deliverable(s), {len(result['unchanged'])} unchanged, "
          f"{n} review file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
