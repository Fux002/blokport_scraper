"""The texture generator pays FAL once per image (dev 2026-11-12, job 8ea6ac51: a missing {Key}/ folder made save_image
fail, and generate_one re-bought the same FLUX.2 [max] image 4 times). The paid call is made ONCE per item and never
retried; only the free half after it (the result's download and the local save) is retried, each retry reusing the
one result. A failed paid call fails the item loud (logged, failures.jsonl, the job's non-zero exit) and the next
produce re-queues it. Hermetic: fal_client and requests are stubbed, the backoff sleep is a no-op. No em dashes.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

KEY = "quartzite__brazilian_white_b2e0a536-7b53-5d1d-8f55-42ac11bf18d9"
ITEM = {"output_name": f"{KEY}/slab", "key": KEY, "category": "slab", "prompt": "Transform this into Quartzite: X.",
        "base_image_url": "https://example.invalid/base_slab.jpg"}
failures: list[str] = []                              # what record_failure was handed, per test


@pytest.fixture
def gen(monkeypatch, tmp_path):
    """The generator module, imported with its GPU-image deps stubbed, writing under tmp_path."""
    for name in ("fal_client", "requests"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.chdir(tmp_path)                       # the module makes ./images at import
    monkeypatch.delitem(sys.modules, "image_pipeline.genetate_images", raising=False)
    module = importlib.import_module("image_pipeline.genetate_images")
    monkeypatch.setattr(module, "OUTPUT_DIR", tmp_path / "images")
    monkeypatch.setattr(module, "time", types.SimpleNamespace(sleep=lambda s: None))
    monkeypatch.setattr(module, "record_failure", lambda item, error: failures.append(error))
    failures.clear()
    return module


class _Fal:
    """Counts the paid calls; returns one image URL, or raises `error`."""

    def __init__(self, error: Exception | None = None):
        self.calls, self._error = 0, error

    def subscribe(self, endpoint, arguments, **kwargs):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return {"images": [{"url": "https://fal.invalid/result.png"}]}


class _Http:
    """The result download: counts the gets and serves the same bytes."""

    def __init__(self):
        self.gets = 0

    def get(self, url, timeout):
        self.gets += 1
        return types.SimpleNamespace(content=b"png", raise_for_status=lambda: None)


def _wire(monkeypatch, gen, fal, http, save):
    monkeypatch.setattr(gen, "fal_client", fal)
    monkeypatch.setattr(gen, "requests", http)
    monkeypatch.setattr(gen, "save_image", save)


def test_a_failed_save_is_retried_without_paying_for_the_image_again(monkeypatch, gen):
    fal, http, saves = _Fal(), _Http(), []

    def save(content, out_path):
        saves.append(out_path)
        if len(saves) < 3:
            raise FileNotFoundError(f"No such file or directory: '{out_path}.tmp'")
    _wire(monkeypatch, gen, fal, http, save)
    assert gen.generate_one(dict(ITEM), {}) is True
    assert fal.calls == 1 and len(saves) == 3 and failures == []


def test_a_save_that_never_succeeds_fails_the_item_after_one_paid_call(monkeypatch, gen):
    fal, http = _Fal(), _Http()

    def save(content, out_path):
        raise FileNotFoundError(f"No such file or directory: '{out_path}.tmp'")
    _wire(monkeypatch, gen, fal, http, save)
    assert gen.generate_one(dict(ITEM), {}) is False
    assert fal.calls == 1 and http.gets == gen.MAX_RETRIES and len(failures) == 1


def test_a_failed_paid_call_is_never_retried(monkeypatch, gen):
    fal, http = _Fal(error=TimeoutError("FAL client timeout")), _Http()
    _wire(monkeypatch, gen, fal, http, lambda content, out_path: pytest.fail("saved without an image"))
    assert gen.generate_one(dict(ITEM), {}) is False
    assert fal.calls == 1 and http.gets == 0 and failures == ["FAL client timeout"]
