"""A fake S3 holding the best-model marker as S3 does: every write gets a new ETag, and a conditional write (IfMatch on
the ETag read, IfNoneMatch="*" for an absent object) is refused with PreconditionFailed when the object moved on."""

from __future__ import annotations

import io
import json

from botocore.exceptions import ClientError


def client_error(code, op="GetObject"):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class MarkerS3:
    def __init__(self, keys=None, error=None, body=None, refuse=None):
        # keys: the marker's Keys (None: absent); body: raw bytes instead; error: what every read raises;
        # refuse: the code every write is refused with (a write that never wins)
        self.body = body if body is not None else (None if keys is None else json.dumps(sorted(keys)).encode())
        self.error, self.refuse, self.version, self.puts = error, refuse, 0, []
        self.after_read = None          # called once, right after the next read: another writer in between

    def keys(self):
        return set(json.loads(self.body))

    def _etag(self):
        return f'"v{self.version}"'

    def get_object(self, Bucket, Key):
        if self.error:
            raise self.error
        seen = None if self.body is None else {"Body": io.BytesIO(self.body), "ETag": self._etag()}
        hook, self.after_read = self.after_read, None
        if hook:
            hook()
        if seen is None:
            raise client_error("NoSuchKey")
        return seen

    def put_object(self, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, **_):
        if self.refuse:
            raise client_error(self.refuse, "PutObject")
        if IfNoneMatch == "*" and self.body is not None:
            raise client_error("PreconditionFailed", "PutObject")
        if IfMatch is not None and (self.body is None or IfMatch != self._etag()):
            raise client_error("NoSuchKey" if self.body is None else "PreconditionFailed", "PutObject")
        self.body, self.version = Body, self.version + 1
        self.puts.append(json.loads(Body))
        return {"ETag": self._etag()}
