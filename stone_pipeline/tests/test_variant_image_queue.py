"""Variant icon queue: an icon is queued ONLY for a (Key, category) pair that has a PRODUCT to list (spec rev 8
section 4). A new variety's additions post lists the categories its products sit in; an existing variety's pairs
come from the products file. A category with no product stays an imageless record (Blokport placeholders it) until
its own product appears, and a pair whose icon is already on S3 is never minted again.
"""

from __future__ import annotations

import json

from stone_pipeline.stages import image_prompts as ip

K = "marble__new_stone_00000000-1111-5222-8333-944455556666"


def _queued(out) -> list[str]:
    return [i["output_name"] for i in json.loads(out.read_text(encoding="utf-8"))]


def _no_existing(monkeypatch, icons=frozenset()):
    monkeypatch.setattr(ip, "product_backed_pairs", lambda: set())
    monkeypatch.setattr(ip, "_existing_icons", lambda: set(icons))


def test_build_queues_only_the_categories_a_new_varietys_products_sit_in(tmp_path, monkeypatch):
    additions = tmp_path / "varieties.json"
    additions.write_text(json.dumps([{"key": K, "stone_type": "Marble", "variant": "New Stone",
                                      "categories": ["slab"]}]), encoding="utf-8")   # a slab product only
    _no_existing(monkeypatch)
    assert _queued(ip.build(additions_file=additions, out_path=tmp_path / "q.json")) == [f"{K}/slab"]


def test_build_queues_an_existing_variety_s_product_backed_pair_without_an_icon(tmp_path, monkeypatch):
    # a variety that GAINED a product in a category AFTER it was first created is never in the additions, so the
    # additions queue alone misses it: the product-backed pairs queue it
    additions = tmp_path / "varieties.json"
    additions.write_text("[]", encoding="utf-8")
    _no_existing(monkeypatch)
    monkeypatch.setattr(ip, "product_backed_pairs", lambda: {(K, "block")})
    monkeypatch.setattr(ip, "_variants", lambda: {K: {"Key": K, "Name": "Mani White", "Type": "Granite"}})
    monkeypatch.setattr(ip, "_backbone_types", lambda: {K: "Granite"})
    assert _queued(ip.build(additions_file=additions, out_path=tmp_path / "q.json")) == [f"{K}/block"]


def test_build_does_not_re_mint_a_pair_whose_icon_is_on_s3(tmp_path, monkeypatch):
    additions = tmp_path / "varieties.json"
    additions.write_text("[]", encoding="utf-8")
    _no_existing(monkeypatch, icons={f"{K}/slab"})
    monkeypatch.setattr(ip, "product_backed_pairs", lambda: {(K, "slab")})
    monkeypatch.setattr(ip, "_variants", lambda: {K: {"Key": K, "Name": "Mani White", "Type": "Granite"}})
    monkeypatch.setattr(ip, "_backbone_types", lambda: {K: "Granite"})
    assert _queued(ip.build(additions_file=additions, out_path=tmp_path / "q.json")) == []   # a refresh, not a mint
