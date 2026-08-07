"""Image storage backends (section 7 Stage 7, section 13A.2).

One small interface, two implementations, so the staging location is a config
switch, not a code change:

  - LocalStorageBackend: writes content-addressed bytes under a local staging
    directory. Used now, before S3 credentials exist.
  - S3StorageBackend: uploads the same content-addressed key to a staging bucket.
    Dropped in later with no change to the image stage or the emitted URLs.

The key and the public URL are computed the same way for both backends, so the
emitted CSV references identical URLs whichever backend runs. The workflow the
plan and the operator want is: stage images into a bucket first, then import the
product referencing the staged URL. The local backend mirrors that two-phase
shape; syncing the local staging dir to the bucket later makes the URLs resolve.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Protocol, runtime_checkable

from stone_pipeline.core import logfmt

log = logfmt.get_logger("io.storage")

# The ONLY S3 error codes that mean "the object genuinely does not exist" (head_object returns 404,
# get_object returns NoSuchKey). Every other error -- AccessDenied, throttling, a 5xx, a connection reset
# -- is a real failure that must fail loud, never masquerade as absence (a silent absence read causes a
# re-upload/re-process or, for the image manifest, a wipe). Single source of truth, shared with treat.py.
_S3_MISSING_CODES = ("404", "NoSuchKey")


def s3_error_is_missing(exc: Exception) -> bool:
    """True iff a boto/S3 exception means the object is genuinely absent (404 / NoSuchKey), as opposed to
    a transient or permission error. Callers return None/False on absence and RAISE on everything else."""
    code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
    return code in _S3_MISSING_CODES


def content_key(src_site: str, sha256: str, ext: str = "jpg") -> str:
    """Deterministic, content-addressed key. No uuid (section 11.1). A re-run
    re-derives the same key and is a no-op."""
    return f"{src_site}/{sha256}.{ext}"


@runtime_checkable
class StorageBackend(Protocol):
    def exists(self, key: str) -> bool: ...
    def get(self, key: str) -> Optional[bytes]: ...
    # overwrite=False keeps the write-once guarantee for content-addressed images
    # (same key == same bytes); the mutable url->key manifest passes overwrite=True.
    def put(self, key: str, data: bytes, content_type: str = "image/jpeg", overwrite: bool = False) -> str: ...
    def url_for(self, key: str) -> str: ...


class LocalStorageBackend:
    """Stores bytes under <root>/staging/<key>; returns <public_base><key>.

    public_base defaults to the eventual S3 public base so the emitted URLs match
    what they will be once the staging dir is synced to the bucket."""

    def __init__(self, root: Path, public_base: str, staging_prefix: str = "staging"):
        self.root = Path(root)
        self.public_base = public_base if public_base.endswith("/") else public_base + "/"
        self.staging_prefix = staging_prefix.strip("/")

    def _path(self, key: str) -> Path:
        return self.root / self.staging_prefix / key

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def get(self, key: str) -> Optional[bytes]:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg", overwrite: bool = False) -> str:
        path = self._path(key)
        if overwrite or not path.exists():  # write-once for images; overwrite for the manifest
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return self.url_for(key)

    def url_for(self, key: str) -> str:
        return f"{self.public_base}{key}"


class S3StorageBackend:
    """Uploads to a staging bucket. boto3 is imported lazily so the package runs
    without it. dry_run derives keys and URLs but performs no network IO."""

    def __init__(self, bucket: str, region: str, key_prefix: str, public_base: str,
                 profile: str = "default", dry_run: bool = True):
        self.bucket = bucket
        self.region = region
        self.key_prefix = key_prefix.strip("/")
        self.public_base = public_base if public_base.endswith("/") else public_base + "/"
        self.profile = profile
        self.dry_run = dry_run
        self._client = None

    def _full_key(self, key: str) -> str:
        return f"{self.key_prefix}/{key}" if self.key_prefix else key

    def _get_client(self):
        if self._client is None:
            import boto3  # lazy

            # Empty profile -> default credential chain (the ECS task IAM role on AWS,
            # or ambient creds locally). A named profile is used only if one is set;
            # on Fargate there is no profile, so profile_name="default" would raise
            # ProfileNotFound.
            if self.profile:
                session = boto3.Session(profile_name=self.profile, region_name=self.region)
            else:
                session = boto3.Session(region_name=self.region)
            self._client = session.client("s3")
        return self._client

    def exists(self, key: str) -> bool:
        if self.dry_run:
            return False
        client = self._get_client()
        try:
            client.head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except Exception as exc:
            if s3_error_is_missing(exc):
                return False
            log.error("S3 head_object failed (not a missing-object error); failing loud",
                      extra={"extra_fields": {"key": self._full_key(key), "error": str(exc)}})
            raise

    def get(self, key: str) -> Optional[bytes]:
        if self.dry_run:
            return None
        try:
            resp = self._get_client().get_object(Bucket=self.bucket, Key=self._full_key(key))
            return resp["Body"].read()
        except Exception as exc:
            if s3_error_is_missing(exc):
                return None
            log.error("S3 get_object failed (not a missing-object error); failing loud",
                      extra={"extra_fields": {"key": self._full_key(key), "error": str(exc)}})
            raise

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg", overwrite: bool = False) -> str:
        if not self.dry_run and (overwrite or not self.exists(key)):
            self._get_client().put_object(
                Bucket=self.bucket, Key=self._full_key(key), Body=data, ContentType=content_type
            )
        return self.url_for(key)

    def url_for(self, key: str) -> str:
        return f"{self.public_base}{key}"
