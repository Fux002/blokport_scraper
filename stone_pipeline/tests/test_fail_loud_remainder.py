"""F9 remainder: fail loud where a swallow masked a real failure, and stop dropping a flag's detail.

  F9(b) S3 helpers  -- an AccessDenied (IAM misconfig) must PROPAGATE, not collapse into a benign path
                       (advertise a 404 image link / default a colour to Natural).
  F9(c) ReviewFlag  -- callers pass detail=; the model dropped it silently (pydantic extra='ignore').
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import Confidence, FlagCode, ReviewFlag
from stone_pipeline.stages import emit_catalog


class _FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def test_s3_access_denied_detects_permission_errors_only():
    assert emit_catalog._s3_access_denied(_FakeClientError("AccessDenied")) is True
    assert emit_catalog._s3_access_denied(_FakeClientError("InvalidAccessKeyId")) is True
    assert emit_catalog._s3_access_denied(_FakeClientError("SlowDown")) is False   # transient -> degrade
    assert emit_catalog._s3_access_denied(ImportError("no boto3")) is False        # missing client -> degrade
    assert emit_catalog._s3_access_denied(RuntimeError("boom")) is False


def test_variation_keys_raises_on_access_denied(monkeypatch):
    # a real IAM misconfig must fail loud, not return None (which makes products advertise 404 image links)
    def denied(*a, **k):
        raise _FakeClientError("AccessDenied")

    class _Boto:                                  # boto3's two ways to a client (io/storage.aws_client takes either)
        client = staticmethod(denied)

        class Session:
            def __init__(self, **k): ...
            client = staticmethod(denied)
    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto)
    with pytest.raises(_FakeClientError):
        emit_catalog._s3_variation_keys()


def test_variation_keys_degrades_to_none_on_transient(monkeypatch):
    def slow(*a, **k):
        raise _FakeClientError("SlowDown")             # transient -> fall back, no crash

    class _Boto:
        client = staticmethod(slow)

        class Session:
            def __init__(self, **k): ...
            client = staticmethod(slow)
    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto)
    assert emit_catalog._s3_variation_keys() is None


def test_review_flag_keeps_its_detail():
    f = ReviewFlag(field="inventory", code=FlagCode.stock_undetermined, confidence=Confidence.low,
                   detail="stock could not be determined or derived (no stock signal in the scrape)")
    assert f.detail == "stock could not be determined or derived (no stock signal in the scrape)"   # not dropped


def test_variation_keys_without_boto3_says_so(monkeypatch, caplog):
    # #571 review: a missing boto3 was logged as "S3 unreachable", sending the reader to the network. Same fallback
    # (no listing, the heuristic decides), named for what failed.
    monkeypatch.setitem(__import__("sys").modules, "boto3", None)       # `import boto3` raises ModuleNotFoundError
    assert emit_catalog._s3_variation_keys() is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("boto3" in m for m in messages) and not any("unreachable" in m for m in messages)
