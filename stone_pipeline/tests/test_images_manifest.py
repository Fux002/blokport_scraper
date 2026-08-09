"""GAP G4: _save_manifest is best-effort for a GENUINE persist failure (the manifest is recoverable -- the
next run re-derives it and content-addressed images are not re-uploaded), but must NOT swallow a programming
error (a non-serializable manifest, or an unexpected exception), which is unrecoverable and has to fail loud."""

from __future__ import annotations

import botocore.exceptions as be
import pytest

from stone_pipeline.stages import images


class _Backend:
    """A storage backend whose put() raises a preset exception (or succeeds when exc is None)."""

    def __init__(self, exc=None):
        self._exc = exc

    def put(self, key, data, content_type="image/jpeg", overwrite=False):
        if self._exc is not None:
            raise self._exc
        return "url"


def test_save_manifest_swallows_a_transient_s3_error():
    # a boto/S3 write failure is recoverable (next run re-derives the manifest) -> best-effort, no raise
    transient = be.ClientError({"Error": {"Code": "SlowDown"}}, "PutObject")
    images._save_manifest(_Backend(transient), {"u": "v"})


def test_save_manifest_swallows_a_local_disk_error():
    images._save_manifest(_Backend(OSError("disk full")), {"u": "v"})   # recoverable -> best-effort


def test_save_manifest_fails_loud_on_an_unexpected_error():
    # not a boto/OS persist error (e.g. a bug) -> must propagate, never be masked as a persist failure
    with pytest.raises(RuntimeError):
        images._save_manifest(_Backend(RuntimeError("bug")), {"u": "v"})


def test_save_manifest_fails_loud_on_a_non_serializable_manifest():
    # json.dumps runs BEFORE the persist try, so a non-serializable manifest is a loud bug, not a swallowed put
    with pytest.raises(TypeError):
        images._save_manifest(_Backend(), {"u": object()})
