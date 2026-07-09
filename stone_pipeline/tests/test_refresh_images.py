"""The one-time product-backed image refresh: selection (product-backed AND imaged AND not-yet-refreshed),
the tolerant durable marker, and the cost estimate. The actual FAL generation + S3 upload reuse the
existing image_pipeline/upload runners and are not exercised here (no FAL_KEY/S3 in CI)."""

from __future__ import annotations

import json

from stone_pipeline import refresh_images as ri
from stone_pipeline.stages import image_prompts as ip


def test_build_refresh_selects_only_product_backed_imaged_and_unrefreshed(tmp_path, monkeypatch):
    # a = backed + imaged + fresh -> refresh; b = backed but NO image (build()'s job) -> skip;
    # c = backed + imaged but ALREADY refreshed -> skip; d = imaged but NOT product-backed -> skip.
    monkeypatch.setattr(ip, "product_backed_keys",
                        lambda: {"slab_granite_a_1", "slab_granite_b_2", "slab_granite_c_3"})
    monkeypatch.setattr(ip, "_variants", lambda: {
        "slab_granite_a_1": {"Key": "slab_granite_a_1", "Name": "A", "Image": "https://s3/a.png"},
        "slab_granite_b_2": {"Key": "slab_granite_b_2", "Name": "B", "Image": ""},
        "slab_granite_c_3": {"Key": "slab_granite_c_3", "Name": "C", "Image": "https://s3/c.png"},
        "slab_granite_d_4": {"Key": "slab_granite_d_4", "Name": "D", "Image": "https://s3/d.png"},
    })
    monkeypatch.setattr(ip, "_backbone_types", lambda: {})
    out = tmp_path / "prompts.json"
    ip.build_refresh({"slab_granite_c_3"}, out_path=out)
    keys = [i["output_name"] for i in json.loads(out.read_text(encoding="utf-8"))]
    assert keys == ["slab_granite_a_1"]


def test_parse_marker_is_tolerant():
    assert ri.parse_marker(json.dumps(["k1", "k2"])) == {"k1", "k2"}
    assert ri.parse_marker(b"") == set()                        # no marker yet -> first run
    assert ri.parse_marker("not json at all") == set()          # corrupt -> eligible, not a crash
    assert ri.parse_marker(json.dumps({"not": "a list"})) == set()


def test_plan_costs_per_image(tmp_path, monkeypatch):
    q = tmp_path / "q.json"
    q.write_text(json.dumps([{"output_name": "k1"}, {"output_name": "k2"}]), encoding="utf-8")
    monkeypatch.setattr(ip, "build_refresh", lambda refreshed: q)
    targets, cost = ri.plan(set())
    assert targets == ["k1", "k2"]
    assert cost == round(2 * ri.PER_IMAGE_USD, 2)


def test_save_refreshed_is_a_noop_without_s3(monkeypatch):
    # no client -> nothing persisted (a dry run / CI never writes a marker), reported as False
    assert ri.save_refreshed({"k1"}, client=None) is False
    assert ri.load_refreshed(client=None) == set()
