"""The produce side of the icon layout `{ENV_SEGMENT}/variations/{Key}/{category name}.png` (spec section 4,
"Texture generation per pair"). A texture is ONE (Key, category) pair:

- a queue item carries its pair (`key`, `category`) and its pair id `"{Key}/{category}"` as `output_name` (the icon's
  path under variations/ without ".png", the `_refreshed.json` id);
- "product-backed" is per pair, from (variation id, `Product Category 1`) in the products file; a new mint is queued
  for each category its additions post lists; a held (key-less) addition is never queued;
- "already has an icon" and "has a texture to upgrade" are per pair, from the S3 listing, never an `Image` cell;
- the upload, the local outputs and the best-model marker are per pair;
- the generator takes the category from the item and refuses one it has no prompt for (never a silent slab);
- a re-typed variety's icons move pair by pair.
Every pair path is built and parsed through io.variety_icons, never by hand.
"""

from __future__ import annotations

import csv
import json

import pytest

from stone_pipeline import prepare_variant_images as pvi
from stone_pipeline import refresh_images
from stone_pipeline.config.categories import category
from stone_pipeline.io import variety_icons
from stone_pipeline.stages import image_prompts as ip

_K = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666"


def _queue(path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_pair_id_is_the_icon_path_without_its_prefix_and_suffix():
    assert variety_icons.pair_id(_K, "block") == f"{_K}/block"
    assert variety_icons.icon_key(_K, "block") == f"{variety_icons.VARIATIONS_PREFIX}{_K}/block.png"
    key, cat = variety_icons.pair_of(f"{_K}/tile")
    assert (key, cat.name) == (_K, "tile")
    for bad in (_K, f"{_K}/tiles", "slab_granite_x/slab"):
        with pytest.raises(ValueError):
            variety_icons.pair_of(bad)


def test_a_queue_item_carries_its_pair():
    item = ip._prompt_item(_K, "block", "Granite", "Absolute Black")
    assert (item["output_name"], item["key"], item["category"]) == (f"{_K}/block", _K, "block")
    assert item["base_image_url"] == category("block").base_image and "Granite: Absolute Black" in item["prompt"]


def test_a_new_mint_is_queued_for_each_category_its_additions_post_lists(tmp_path, monkeypatch):
    additions = tmp_path / "varieties.json"
    additions.write_text(json.dumps([
        {"key": _K, "variant": "Absolute Black", "stone_type": "Granite", "categories": ["block", "slab"]},
        {"key": "", "variant": "Avocatus", "stone_type": "", "categories": ["slab"]}]), encoding="utf-8")
    monkeypatch.setattr(ip, "product_backed_pairs", lambda: set())
    monkeypatch.setattr(ip, "_existing_icons", lambda: set())
    out = ip.build(additions_file=additions, out_path=tmp_path / "q.json")
    assert [i["output_name"] for i in _queue(out)] == [f"{_K}/block", f"{_K}/slab"]   # the held post is never queued


def test_an_existing_product_backed_pair_is_queued_only_without_an_icon(tmp_path, monkeypatch):
    additions = tmp_path / "varieties.json"
    additions.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(ip, "product_backed_pairs", lambda: {(_K, "slab"), (_K, "tile")})
    monkeypatch.setattr(ip, "_existing_icons", lambda: {f"{_K}/slab"})            # the slab icon is on S3
    monkeypatch.setattr(ip, "_variants", lambda: {_K: {"Key": _K, "Name": "Absolute Black", "Type": "Granite"}})
    monkeypatch.setattr(ip, "_backbone_types", lambda: {_K: "Granite"})
    out = ip.build(additions_file=additions, out_path=tmp_path / "q.json")
    assert [i["output_name"] for i in _queue(out)] == [f"{_K}/tile"]


def test_product_backed_is_decided_per_pair_from_the_products_category(tmp_path, monkeypatch):
    export = tmp_path / "variants_export.csv"
    export.write_text(f"Id,Key,Name,Aliases,status,Type,owner\nvar_1,{_K},Absolute Black,,active,Granite,scraper\n",
                      encoding="utf-8")
    products = tmp_path / "3_products_all.csv"
    products.write_text("STN Variation Id,Product Category 1\nvar_1,pcat_blocks\nvar_1,pcat_blocks\n", encoding="utf-8")
    monkeypatch.setattr(ip, "_products_file", lambda: products)
    monkeypatch.setattr(ip, "_export_file", lambda: export)
    monkeypatch.setattr(ip, "_category_of_pcat", lambda: {"pcat_blocks": "block", "pcat_slabs": "slab"})
    assert ip.product_backed_pairs() == {(_K, "block")}


def test_an_upgrade_is_a_product_backed_pair_with_an_icon_not_yet_refreshed(monkeypatch):
    monkeypatch.setattr(ip, "product_backed_pairs", lambda: {(_K, "slab"), (_K, "block"), (_K, "tile")})
    icons = {f"{_K}/slab", f"{_K}/block"}
    assert ip.upgrade_candidates({f"{_K}/block"}, cap=5, icons=icons) == [f"{_K}/slab"]
    assert ip.upgrade_candidates(set(), cap=5, icons=None) == []                    # S3 unknown: nothing proven


def test_regenerating_named_icons_takes_pair_ids_never_a_bare_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ip, "_variants", lambda: {_K: {"Key": _K, "Name": "Absolute Black", "Type": "Granite"}})
    monkeypatch.setattr(ip, "_backbone_types", lambda: {_K: "Granite"})
    out = ip.build_for_keys([f"{_K}/tile"], out_path=tmp_path / "q.json")
    assert [i["output_name"] for i in _queue(out)] == [f"{_K}/tile"]
    with pytest.raises(ValueError, match="pair"):
        ip.build_for_keys([_K], out_path=tmp_path / "q.json")


def test_the_re_dispatch_prune_drops_pairs_already_on_s3_but_keeps_upgrades(tmp_path, monkeypatch):
    queue = tmp_path / "q.json"
    queue.write_text(json.dumps([{"output_name": f"{_K}/slab"}, {"output_name": f"{_K}/block"},
                                 {"output_name": f"{_K}/tile", "mode": ip.UPGRADE_MODE}]), encoding="utf-8")
    monkeypatch.setattr(pvi, "_existing_icons", lambda: {f"{_K}/slab", f"{_K}/tile"})
    kept = pvi._prune_already_on_s3(queue, [f"{_K}/slab", f"{_K}/block", f"{_K}/tile"])
    assert kept == [f"{_K}/block", f"{_K}/tile"]
    assert [i["output_name"] for i in _queue(queue)] == [f"{_K}/block", f"{_K}/tile"]


class _Client:
    def __init__(self):
        self.uploaded = []

    def upload_file(self, filename, bucket, key):
        self.uploaded.append((filename, key))


def test_a_generated_pair_uploads_to_its_icon_key(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(pvi, "SETTINGS", types.SimpleNamespace(s3=types.SimpleNamespace(dry_run=False, bucket="b")))
    monkeypatch.setattr(pvi, "DEBG_OUTPUT_DIR", tmp_path)
    (tmp_path / _K).mkdir()
    (tmp_path / _K / "block.png").write_bytes(b"png")
    client = _Client()
    uploaded, missing = pvi.upload_variant_images([f"{_K}/block", f"{_K}/slab"], client=client)
    assert (uploaded, missing) == (1, [f"{_K}/slab"])
    assert client.uploaded == [(str(tmp_path / _K / "block.png"), variety_icons.icon_key(_K, "block"))]


def test_the_best_model_marker_holds_pair_ids():
    assert refresh_images.parse_marker(json.dumps([f"{_K}/slab", f"{_K}/block"])) == {f"{_K}/slab", f"{_K}/block"}
    with pytest.raises(refresh_images.MarkerUnreadable):
        refresh_images.parse_marker(json.dumps([_K]))                             # a flat Key is not a pair


def test_the_generator_takes_the_category_from_the_item_and_refuses_an_unknown_one(monkeypatch):
    import importlib
    import sys
    import types
    for name in ("fal_client", "requests"):                 # the GPU image's deps, not CI's: stubbed for the import
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.delitem(sys.modules, "image_pipeline.genetate_images", raising=False)
    gen = importlib.import_module("image_pipeline.genetate_images")
    assert gen.item_category({"output_name": f"{_K}/block", "key": _K, "category": "block"}) == "block"
    with pytest.raises(ValueError, match="no prompt"):
        gen.item_category({"output_name": f"{_K}/panel", "key": _K, "category": "panel"})


def test_a_re_typed_varietys_icons_move_pair_by_pair(monkeypatch):
    from stone_pipeline import catalog
    new = "marble__absolute_black_00000000-1111-5222-8333-944455556666"
    copied = []
    moved = catalog.copy_variety_icons(_K, new, existing={_K: {"slab": "e1", "block": "e2"}, new: {"slab": "e3"}},
                                       copier=lambda old_key, new_key: copied.append((old_key, new_key)) or True)
    assert copied == [(variety_icons.icon_key(_K, "block"), variety_icons.icon_key(new, "block"))]
    assert moved == 1                                                               # the slab icon is already there


def test_the_intake_renames_an_inbox_image_to_its_pair(tmp_path, monkeypatch):
    # the inbox folder names the category; an old-style name ({branch}_rb_<id>_<type>_<name>) or the bare Key joins
    # the variety; the copy lands as renamed/{Key}/{category}.png, and a product-backed pair with no icon is reported
    from stone_pipeline.stages import image_intake
    export = tmp_path / "variants_export.csv"
    export.write_text(f"Id,Key,Name,Aliases,status,Type,owner\nvar_1,{_K},Absolute Black,,active,Granite,scraper\n",
                      encoding="utf-8")
    monkeypatch.setattr(image_intake, "_variant_files", lambda: [export])
    monkeypatch.setattr(image_intake, "_existing_icons", lambda: {f"{_K}/slab"})
    monkeypatch.setattr(image_intake, "product_backed_pairs", lambda: {(_K, "slab"), (_K, "block"), (_K, "tile")})
    (tmp_path / "inbox" / "blocks").mkdir(parents=True)
    (tmp_path / "inbox" / "blocks" / "block_rb_7_granite_absolute_black.png").write_bytes(b"png")
    res = image_intake.run(inbox_root=tmp_path / "inbox", output_dir=tmp_path / "renamed",
                           reports_dir=tmp_path / "reports")
    assert (tmp_path / "renamed" / _K / "block.png").read_bytes() == b"png" and res["renamed"] == 1
    with (tmp_path / "reports" / "images_to_generate.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [(r["Key"], r["category"]) for r in rows] == [(_K, "tile")]     # slab is on S3, block came in
