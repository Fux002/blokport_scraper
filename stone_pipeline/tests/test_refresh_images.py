"""The durable best-model marker of the variant textures. Only a truly absent marker reads as empty (a first run);
one that exists but cannot be read raises, and is never rewritten: read as empty, the next save would wipe it and
the upgrade drip would pay again for every texture already made by the best model."""

from __future__ import annotations

import io
import json

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from stone_pipeline import refresh_images as ri


class _S3:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.puts = body, error, []

    def get_object(self, Bucket, Key):
        if self.error:
            raise self.error
        return {"Body": io.BytesIO(self.body)}

    def put_object(self, **kw):
        self.puts.append(json.loads(kw["Body"]))


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "GetObject")


def test_the_marker_reads_its_keys():
    assert ri.load_refreshed(_S3(body=json.dumps(["k1", "k2"]).encode())) == {"k1", "k2"}


def test_only_an_absent_marker_reads_as_empty():
    assert ri.load_refreshed(_S3(error=_client_error("NoSuchKey"))) == set()        # a first run


@pytest.mark.parametrize("s3", [
    _S3(error=_client_error("AccessDenied")),
    _S3(error=EndpointConnectionError(endpoint_url="https://s3")),
    _S3(body=b"not json at all"),
    _S3(body=json.dumps({"not": "a list"}).encode()),
], ids=["denied", "unreachable", "corrupt", "not-a-list"])
def test_a_marker_that_exists_but_cannot_be_read_raises(s3):
    with pytest.raises(ri.MarkerUnreadable):
        ri.load_refreshed(s3)


def test_an_unreadable_marker_is_never_rewritten(monkeypatch):
    monkeypatch.setattr(ri, "SETTINGS", type("S", (), {"s3": type("S3", (), {"bucket": "b", "dry_run": False})})())
    s3 = _S3(error=_client_error("AccessDenied"))
    assert ri.mark_best_model(["k3"], client=s3) is False
    assert s3.puts == []                                                            # not wiped to {"k3"}


def test_marking_merges_into_the_existing_marker(monkeypatch):
    monkeypatch.setattr(ri, "SETTINGS", type("S", (), {"s3": type("S3", (), {"bucket": "b", "dry_run": False})})())
    s3 = _S3(body=json.dumps(["k1"]).encode())
    assert ri.mark_best_model(["k2"], client=s3) is True
    assert s3.puts == [["k1", "k2"]]


def test_save_refreshed_is_a_noop_without_s3():
    assert ri.save_refreshed({"k1"}, client=None) is False
