"""S3StorageBackend must distinguish a genuine absence (404 / NoSuchKey -> None/False) from a real
transient/permission error (-> fail loud). A silent absence read on a real error causes needless
re-uploads/re-processing and, for the image manifest, a full wipe -- so the error must surface."""

from __future__ import annotations

import pytest

from stone_pipeline.io.storage import S3StorageBackend, s3_error_is_missing


class _S3Error(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


class _FakeClient:
    """Raises the configured error on every head/get, mimicking boto3's ClientError shape."""

    def __init__(self, code: str):
        self._code = code

    def head_object(self, **_kw):
        raise _S3Error(self._code)

    def get_object(self, **_kw):
        raise _S3Error(self._code)


def _backend(code: str) -> S3StorageBackend:
    b = S3StorageBackend(bucket="b", region="r", key_prefix="p", public_base="https://x/", dry_run=False)
    b._client = _FakeClient(code)   # inject; _get_client() returns it since it is already set
    return b


def test_s3_error_is_missing_only_for_absence_codes():
    assert s3_error_is_missing(_S3Error("404"))
    assert s3_error_is_missing(_S3Error("NoSuchKey"))
    assert not s3_error_is_missing(_S3Error("AccessDenied"))
    assert not s3_error_is_missing(_S3Error("SlowDown"))
    assert not s3_error_is_missing(RuntimeError("no response attr"))


def test_absence_reads_as_not_present():
    assert _backend("404").exists("k") is False        # head_object 404 -> absent
    assert _backend("NoSuchKey").get("k") is None       # get_object NoSuchKey -> absent


def test_real_error_fails_loud():
    with pytest.raises(_S3Error):
        _backend("AccessDenied").exists("k")            # permission error must not read as absent
    with pytest.raises(_S3Error):
        _backend("SlowDown").get("k")                   # throttling must not read as absent
