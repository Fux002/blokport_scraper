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
    class _Boto:
        class Session:
            def __init__(self, **k): ...
            def client(self, *a, **k):
                raise _FakeClientError("AccessDenied")
    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto)
    with pytest.raises(_FakeClientError):
        emit_catalog._s3_variation_keys()


def test_variation_keys_degrades_to_none_on_transient(monkeypatch):
    class _Boto:
        class Session:
            def __init__(self, **k): ...
            def client(self, *a, **k):
                raise _FakeClientError("SlowDown")     # transient -> fall back, no crash
    monkeypatch.setitem(__import__("sys").modules, "boto3", _Boto)
    assert emit_catalog._s3_variation_keys() is None


def test_review_flag_keeps_its_detail():
    f = ReviewFlag(field="inventory", code=FlagCode.stock_undetermined, confidence=Confidence.low,
                   detail="stock could not be determined or derived (no stock signal in the scrape)")
    assert f.detail == "stock could not be determined or derived (no stock signal in the scrape)"   # not dropped
