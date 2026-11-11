"""SC-4 (plan SC-3-SC-4-PLAN.md sections 2c and 2d, PHASE-1-SPEC section 4): the ledger layer on v15.

A variety has no category. Its served payload is {type, name, aliases, category_images}, where each icon is its
(category handle, full S3 key, ETag) pair from emit's listing, and the hash covers the icons, so an icon re-made in
place re-serves its variety. A product's category is its HANDLE (D11) and its payload has no bundle_size; a product
lists only once its variety has an icon in the product's own category. The icon S3 keys are built in one place.
"""
from __future__ import annotations

import csv
import json

import pytest

from stone_pipeline.config.categories import category_by_handle
from stone_pipeline.io import variety_icons
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.payloads import product_payload, variation_payload
from stone_pipeline.ledger.populate import populate_variations_full
from stone_pipeline.ledger.sync import ready
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.stages import emit_catalog

KEY = new_key("Marble", "Arabescato")
PREFIX = variety_icons.VARIATIONS_PREFIX


def _full(d, rows):
    path = d / "1_variants_full.csv"
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(emit_catalog._COLS)
        w.writerows([[r.get(c, "") for c in emit_catalog._COLS] for r in rows])
    return path


def _sidecar(d, pairs):
    (d / variety_icons.ICON_SIDECAR).write_text(json.dumps(pairs), encoding="utf-8")


def _arabescato(d, pairs):
    _sidecar(d, pairs)
    return _full(d, [{"Key": KEY, "Name": "Arabescato", "Type": "Marble", "Aliases": "Arabescato Vagli"}])


def _row(ledger, key=KEY):
    return ledger.execute("SELECT * FROM variation WHERE key = ?", (key,)).fetchone()


# --- the icon layout -------------------------------------------------------------------------------------------

def test_an_icon_key_is_the_env_prefix_the_key_and_the_category_name():
    assert variety_icons.icon_key(KEY, "block") == f"{PREFIX}{KEY}/block.png"
    assert variety_icons.icon_pair(f"{PREFIX}{KEY}/block.png")[0] == KEY
    assert variety_icons.icon_pair(f"{PREFIX}{KEY}/block.png")[1].handle == "blocks"


@pytest.mark.parametrize("key, name", [("slab_marble_arabescato_1a", "slab"), (KEY, "Slabs"), (KEY, "../slab")])
def test_no_icon_key_is_built_from_a_bad_key_or_a_name_that_is_no_pack_category(key, name):
    with pytest.raises(ValueError):
        variety_icons.icon_key(key, name)


@pytest.mark.parametrize("obj", ["slab_marble_arabescato_1a.png", "_refreshed.json", f"{KEY}/marble.png",
                                 f"{KEY}/slab/x.png"])
def test_an_object_that_is_not_an_icon_pair_is_not_read_as_one(obj):
    assert variety_icons.icon_pair(PREFIX + obj) is None


def test_the_category_handle_is_the_pack_plural():
    assert category_by_handle("slabs").name == "slab" and category_by_handle("Slabs") is None


# --- the variation payload and its hash ------------------------------------------------------------------------

def test_the_variation_payload_has_no_category_and_carries_its_icons_in_handle_order(tmp_path):
    full = _arabescato(tmp_path, {KEY: {"tile": "e-t", "slab": "e-s"}})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, full)
        payload = ready(lg, "variations")[0]["payload"]
    assert payload == {
        "type": "Marble", "name": "Arabescato", "aliases": ["Arabescato Vagli"],
        "category_images": [{"category": "slabs", "s3_key": f"{PREFIX}{KEY}/slab.png", "etag": "e-s"},
                            {"category": "tiles", "s3_key": f"{PREFIX}{KEY}/tile.png", "etag": "e-t"}]}


def test_an_icon_re_made_in_place_re_serves_its_variety_and_an_unchanged_one_does_not(tmp_path):
    full = _arabescato(tmp_path, {KEY: {"slab": "e1"}})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, full)
        lg.execute("UPDATE variation SET state = 'synced' WHERE key = ?", (KEY,))
        populate_variations_full(lg, full)
        assert _row(lg)["state"] == "synced"
        _sidecar(tmp_path, {KEY: {"slab": "e2"}})
        populate_variations_full(lg, full)
        assert _row(lg)["state"] == "dirty"


def test_a_new_icon_pair_re_serves_and_a_pair_gone_from_s3_is_gone_from_the_ledger(tmp_path):
    full = _arabescato(tmp_path, {KEY: {"slab": "e1"}})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, full)
        lg.execute("UPDATE variation SET state = 'synced' WHERE key = ?", (KEY,))
        _sidecar(tmp_path, {KEY: {"slab": "e1", "block": "e9"}})
        populate_variations_full(lg, full)
        assert _row(lg)["state"] == "dirty"
        _sidecar(tmp_path, {})
        populate_variations_full(lg, full)
        assert lg.execute("SELECT COUNT(*) n FROM variation_category_image").fetchone()["n"] == 0


def test_without_a_listing_the_stored_icons_stand_and_nothing_re_serves(tmp_path):
    full = _arabescato(tmp_path, {KEY: {"slab": "e1"}})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, full)
        lg.execute("UPDATE variation SET state = 'synced' WHERE key = ?", (KEY,))
        (tmp_path / variety_icons.ICON_SIDECAR).unlink()
        populate_variations_full(lg, full)
        assert _row(lg)["state"] == "synced"
        assert [r["etag"] for r in lg.execute("SELECT etag FROM variation_category_image")] == ["e1"]


def test_the_type_comes_from_the_variety_file(tmp_path):
    full = _arabescato(tmp_path, {})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, full)
        assert _row(lg)["type"] == "Marble"


@pytest.mark.parametrize("pairs", [{KEY: {"marble": "e1"}}, {KEY: {"slab": ""}}, {KEY: "e1"}])
def test_a_malformed_listing_fails_loud(tmp_path, pairs):
    full = _arabescato(tmp_path, pairs)
    with Ledger.open(tmp_path / "l.db", env="development") as lg, pytest.raises(ValueError):
        populate_variations_full(lg, full)


def test_a_prefixed_key_in_the_variety_file_is_refused(tmp_path):
    full = _full(tmp_path, [{"Key": "slab_marble_arabescato_1a", "Name": "Arabescato", "Type": "Marble"}])
    with Ledger.open(tmp_path / "l.db", env="development") as lg, pytest.raises(ValueError, match="variety Key"):
        populate_variations_full(lg, full)


def test_deleting_a_variety_deletes_its_icons(tmp_path):
    full = _arabescato(tmp_path, {KEY: {"slab": "e1"}})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, full)
        lg.execute("DELETE FROM variation WHERE key = ?", (KEY,))
        assert lg.execute("SELECT COUNT(*) n FROM variation_category_image").fetchone()["n"] == 0


# --- the product payload and the icon gate ---------------------------------------------------------------------

def _product(lg, category):
    now = now_iso()
    lg.upsert("product", {"sku": "P-1", "source": "s", "variation_key": KEY, "category": category,
                          "type": "Marble", "bundle_size": 7, "state": "pending",
                          "created_at": now, "updated_at": now}, pk=("sku",))


def test_a_product_lists_only_once_its_variety_has_an_icon_in_the_product_s_category(tmp_path):
    full = _arabescato(tmp_path, {KEY: {"block": "e1"}})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, full)
        lg.execute("UPDATE variation SET state = 'synced', medusa_id = 'V1' WHERE key = ?", (KEY,))
        _product(lg, "slabs")
        assert ready(lg, "products") == []
        _sidecar(tmp_path, {KEY: {"block": "e1", "slab": "e2"}})
        populate_variations_full(lg, full)
        lg.execute("UPDATE variation SET state = 'synced' WHERE key = ?", (KEY,))
        served = ready(lg, "products")
    assert [p["external_id"] for p in served] == ["P-1"]
    assert served[0]["payload"]["category"] == "slabs"


def test_the_product_payload_has_no_bundle_size_but_the_ledger_keeps_it_for_the_render(tmp_path):
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        populate_variations_full(lg, _arabescato(tmp_path, {}))
        _product(lg, "slabs")
        stored = lg.get("product", "sku", "P-1")
    assert stored["bundle_size"] == 7
    assert "bundle_size" not in product_payload(stored)


def test_a_variation_payload_never_carries_a_category_or_an_image_url():
    v = {"type": "Marble", "name": "Arabescato", "aliases": "[]"}
    assert set(variation_payload(v, [])) == {"type", "name", "aliases", "category_images"}


# --- emit: the listing and the side file -----------------------------------------------------------------------

def _base(d):
    path = d / "variants_export_base.csv"
    with path.open("w", newline="", encoding="utf-8") as h:
        csv.writer(h).writerows([emit_catalog._COLS, [KEY, "Arabescato", "Marble", ""]])
    return path


def test_emit_writes_the_produced_varieties_pairs_beside_the_full_file(tmp_path):
    other = new_key("Granite", "Absolute Black")
    out = tmp_path / "out" / "1_variants_full.csv"
    out.parent.mkdir()
    emit_catalog.build(_base(tmp_path), icons={KEY: {"tile": "e-t", "slab": "e-s"}, other: {"slab": "x"}},
                       out_path=out)
    assert json.loads((out.parent / variety_icons.ICON_SIDECAR).read_text()) == {KEY: {"slab": "e-s", "tile": "e-t"}}


def test_in_production_a_failed_icon_listing_fails_loud(monkeypatch):
    def unreachable(*a, **k):
        raise ConnectionError("unreachable")
    monkeypatch.setattr(emit_catalog, "IS_PRODUCTION", True)
    monkeypatch.setattr(emit_catalog.storage, "aws_client", unreachable)
    with pytest.raises(ConnectionError):
        emit_catalog._s3_icon_pairs()


def test_outside_production_an_unreachable_s3_lists_nothing_and_keeps_the_stored_icons(monkeypatch):
    def unreachable(*a, **k):
        raise ConnectionError("unreachable")
    monkeypatch.setattr(emit_catalog, "IS_PRODUCTION", False)
    monkeypatch.setattr(emit_catalog.storage, "aws_client", unreachable)
    assert emit_catalog._s3_icon_pairs() is None
