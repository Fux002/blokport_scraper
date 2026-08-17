"""GET /config/v1/diagnostics enriches each source with a LIVE `images` progress block, computed at serve
time from the S3 markers (not the produce-time snapshot) so the admin UI's poll actually moves while the
GPU de-watermarks. Pure dispatch tests, boto3 mocked (no AWS).
"""

from __future__ import annotations

import types

from stone_pipeline.config import diagnostics, server

A, B, C = "a" * 64, "b" * 64, "c" * 64


class _Paginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}


class FakeS3:
    def __init__(self, keys):
        self.keys = keys

    def get_paginator(self, _name):
        return _Paginator(self.keys)


def _keys(scraped=(), enhanced=(), discarded=(), src="varsha"):
    k = [f"dev/products/scraped/{src}/{s}.jpg" for s in scraped]
    k += [f"dev/products/enhanced/{src}/{s}.txt" for s in enhanced]
    k += [f"dev/products/discarded/{src}/{s}.json" for s in discarded]
    return k


def _set_mode(monkeypatch, mode):
    # ImagesConfig is a frozen dataclass; replace the whole SETTINGS the helper imports at call time.
    monkeypatch.setattr("stone_pipeline.config.settings.SETTINGS",
                        types.SimpleNamespace(images=types.SimpleNamespace(mode=mode)))


def _wire(monkeypatch, mode, rows, keys):
    _set_mode(monkeypatch, mode)
    monkeypatch.setattr(diagnostics, "read_all", lambda *a, **k: rows)
    monkeypatch.setattr("boto3.client", lambda service, region_name=None: FakeS3(keys))


def test_diagnostics_attaches_live_images_block(monkeypatch):
    rows = [{"source": "varsha", "health": "OK"}]
    _wire(monkeypatch, "s3", rows, _keys(scraped=(A, B, C), enhanced=(A,), discarded=(B,)))
    code, body = server.dispatch("GET", ["diagnostics"], None)
    assert code == 200
    assert body["diagnostics"][0]["images"] == {
        "total": 3, "ready": 1, "held": 1, "generating": True}


def test_no_images_block_when_not_s3_mode(monkeypatch):
    rows = [{"source": "varsha", "health": "OK"}]
    _wire(monkeypatch, "passthrough", rows, _keys(scraped=(A, B, C)))
    _, body = server.dispatch("GET", ["diagnostics"], None)
    assert "images" not in body["diagnostics"][0]


def test_images_omitted_for_source_with_no_scraped(monkeypatch):
    rows = [{"source": "varsha", "health": "OK"}]
    _wire(monkeypatch, "s3", rows, _keys())        # nothing scraped for the source
    _, body = server.dispatch("GET", ["diagnostics"], None)
    assert "images" not in body["diagnostics"][0]


def test_s3_failure_omits_block_never_500s(monkeypatch):
    rows = [{"source": "varsha", "health": "OK"}]
    _set_mode(monkeypatch, "s3")
    monkeypatch.setattr(diagnostics, "read_all", lambda *a, **k: rows)

    def _boom(service, region_name=None):
        raise RuntimeError("s3 unreachable")

    monkeypatch.setattr("boto3.client", _boom)
    code, body = server.dispatch("GET", ["diagnostics"], None)
    assert code == 200
    assert "images" not in body["diagnostics"][0]
