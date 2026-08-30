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
        "total": 3, "ready": 1, "held": 1, "generating": True, "held_for_image": 0}  # no stages -> 0


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


def test_images_block_carries_held_for_image_from_the_run(monkeypatch):
    # held_for_image surfaces the last produce's no_image count (products held because their texture was not
    # ready) into the LIVE images block, so the admin renders "N held -> Republish" alongside the live texture
    # progress -- the reconciliation the frozen per-run 'Produced' count cannot give on its own.
    rows = [{"source": "varsha", "health": "OK",
             "stages": [{"stage": "images", "extra": {"no_image": 5, "staged": 0}}]}]
    _wire(monkeypatch, "s3", rows, _keys(scraped=(A, B, C), enhanced=(A,)))
    _, body = server.dispatch("GET", ["diagnostics"], None)
    assert body["diagnostics"][0]["images"]["held_for_image"] == 5


def test_held_for_image_helper_is_defensive():
    assert diagnostics.held_for_image(
        {"stages": [{"stage": "images", "extra": {"no_image": 7}}]}) == 7
    assert diagnostics.held_for_image({"stages": [{"stage": "match_variation", "extra": {}}]}) == 0  # no images stage
    assert diagnostics.held_for_image({"stages": []}) == 0
    assert diagnostics.held_for_image({}) == 0                                     # no stages key
    assert diagnostics.held_for_image({"stages": [{"stage": "images"}]}) == 0      # no extra
    assert diagnostics.held_for_image(
        {"stages": [{"stage": "images", "extra": {"no_image": "x"}}]}) == 0        # malformed value
    assert diagnostics.held_for_image(None) == 0                                   # not a dict


def test_held_for_image_excludes_permanent_no_source_rejects():
    # held_for_image is the REPUBLISHABLE subset only: no_image MINUS no_image_source (rows with no usable
    # source url at all -- a permanent reject a republish can never fix, so it must not drive a republish).
    def held(no_image, no_source):
        return diagnostics.held_for_image(
            {"stages": [{"stage": "images", "extra": {"no_image": no_image, "no_image_source": no_source}}]})

    assert held(63, 63) == 0     # the live case: every hold is a permanent no-source reject -> clears to 0
    assert held(10, 4) == 6      # 4 permanent no-source excluded; 6 genuinely republishable remain
    assert held(5, 0) == 5       # all transient (image not linked yet) -> all republishable
    assert held(3, 5) == 0       # never negative (guard); no_image_source is a subset of no_image
    # back-compat: a pre-change summary carrying no no_image_source subtracts 0 (old behaviour preserved)
    assert diagnostics.held_for_image(
        {"stages": [{"stage": "images", "extra": {"no_image": 8}}]}) == 8
