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

            session = boto3.Session(profile_name=self.profile, region_name=self.region)
            self._client = session.client("s3")
        return self._client

    def exists(self, key: str) -> bool:
        if self.dry_run:
            return False
        client = self._get_client()
        try:
            client.head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except Exception:
            return False

    def get(self, key: str) -> Optional[bytes]:
        if self.dry_run:
            return None
        try:
            resp = self._get_client().get_object(Bucket=self.bucket, Key=self._full_key(key))
            return resp["Body"].read()
        except Exception:
            return None

    def put(self, key: str, data: bytes, content_type: str = "image/jpeg", overwrite: bool = False) -> str:
        if not self.dry_run and (overwrite or not self.exists(key)):
            self._get_client().put_object(
                Bucket=self.bucket, Key=self._full_key(key), Body=data, ContentType=content_type
            )
        return self.url_for(key)

    def url_for(self, key: str) -> str:
        return f"{self.public_base}{key}"
