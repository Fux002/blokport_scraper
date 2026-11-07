"""GET /config/v1/diagnostics enriches each source with an `images` block counted from the sync ledger at serve time,
over its listable products (in stock, or hidden for want of a standardised photo; a sold-out product is not counted):
total = ready + hidden + the rest. ready: listed with a published standardised photo (with the image pipeline off, with a
hosted photo); hidden: served at stock 0 with reason no_photo; generating: a photos run is under way; and the last
produce's held_for_image. The photo process relinks the products itself when it publishes, so the ledger is current."""

from __future__ import annotations

import pytest

from stone_pipeline.config import store
from stone_pipeline.config.control import diagnostics, runner, server
from stone_pipeline.config.settings import ENV_NAME, ENV_SEGMENT
from stone_pipeline.ledger.db import Ledger, now_iso

BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "ledger.db"))
    store.upsert_row({"source": "sentas", "source_code": "sen", "vendor": "Sentas", "ports": ["Izmir"]})
    store.upsert_row({"source": "varsha", "source_code": "var", "vendor": "Varsha", "ports": ["Brindisi"],
                      "image_pipeline": False})
    return tmp_path / "ledger.db"


def _product(ledger, sku, source, thumbnail, reason=None, qty=3):
    now = now_iso()
    ledger.upsert("product", {"sku": sku, "source": source, "state": "synced", "thumbnail_key": thumbnail,
                              "created_at": now, "updated_at": now}, pk=("sku",))
    ledger.upsert("inventory", {"sku": sku, "qty": 0 if reason else qty, "reason": reason, "updated_at": now}, pk=("sku",))


def _fill(path):
    with Ledger.open(path, env=ENV_NAME) as ledger:
        _product(ledger, "SEN-1", "sen", f"{BASE}/standard/main/sentas/{'a' * 64}.jpg")
        _product(ledger, "SEN-2", "sen", f"{BASE}/improved/sentas/{'b' * 64}.jpg", reason="no_photo")
        _product(ledger, "SEN-3", "sen", "")
        _product(ledger, "SEN-4", "sen", f"{BASE}/standard/main/sentas/{'d' * 64}.jpg", qty=0)   # sold out: not counted
        _product(ledger, "VAR-1", "var", f"{BASE}/improved/varsha/{'c' * 64}.jpg")
        _product(ledger, "VAR-2", "var", "")


def _diagnostics(monkeypatch, rows):
    monkeypatch.setattr(diagnostics, "read_all", lambda *a, **k: rows)
    code, body = server.dispatch("GET", ["diagnostics"], None)
    assert code == 200
    return {r["source"]: r.get("images") for r in body["diagnostics"]}


def test_the_images_block_counts_each_source_from_the_ledger(ledger_path, monkeypatch):
    _fill(ledger_path)
    rows = [{"source": "sentas", "stages": [{"stage": "images", "extra": {"no_image": 2}}]}, {"source": "varsha"}]
    blocks = _diagnostics(monkeypatch, rows)
    assert blocks["sentas"] == {"image_pipeline": True, "total": 3, "ready": 1, "hidden": 1, "generating": False,
                                "held_for_image": 2}
    assert blocks["varsha"] == {"image_pipeline": False, "total": 2, "ready": 1, "hidden": 0, "generating": False,
                                "held_for_image": 0}


def test_generating_is_a_photos_run_under_way_for_a_source_in_the_pipeline(ledger_path, monkeypatch):
    _fill(ledger_path)
    monkeypatch.setattr(runner, "photos_in_progress", lambda: True)
    blocks = _diagnostics(monkeypatch, [{"source": "sentas"}, {"source": "varsha"}])
    assert blocks["sentas"]["generating"] is True and blocks["varsha"]["generating"] is False


def test_a_source_with_no_product_in_the_ledger_has_no_block(ledger_path, monkeypatch):
    _fill(ledger_path)
    assert _diagnostics(monkeypatch, [{"source": "zucchi"}]) == {"zucchi": None}


def test_without_a_ledger_no_block_is_served_and_none_is_created(ledger_path, monkeypatch):
    assert _diagnostics(monkeypatch, [{"source": "sentas"}]) == {"sentas": None}
    assert not ledger_path.exists()


def test_photos_in_progress_is_only_a_running_photos_stage(monkeypatch):
    monkeypatch.setattr(runner, "_current_id", "r1")
    for stage, status, expected in (("photos", "running", True), ("photos", "queued", True),
                                    ("all", "running", False), ("photos", "succeeded", False)):
        monkeypatch.setattr(runner, "_runs", {"r1": {"stage": stage, "status": status}})
        assert runner.photos_in_progress() is expected


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
