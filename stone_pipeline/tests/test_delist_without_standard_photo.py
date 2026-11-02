"""A product is live only with a standardised photo (owner 2026-11-02: whatever has no good image is delisted until it is
fixed): with the rule on, a product the image stage found without one goes out at stock 0 with reason 'no_photo'
(Blokport hides it; always stock 0, since Blokport reads an unknown reason above 0 as a plain quantity) and comes back on the first produce after its photo is published. A run that cannot tell (the photo
pool unreadable, or the stock-only refresh, which skips the image stage) never changes whether a product is hidden, and a
pool that is empty or unreadable (local mode, nothing published, a failed read) means "cannot tell". Hiding and showing
again are served to Blokport even when the stock stays 0; the dead-stock purge never deletes a hidden product, and a
bundle never redirects to a lot whose pieces are all hidden."""
from __future__ import annotations

import dataclasses
import json

import pytest

from stone_pipeline.config.settings import ENV_SEGMENT, SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger import populate
from stone_pipeline.ledger.populate import populate_discontinued, populate_inventory
from stone_pipeline.ledger.sync import purge_discontinued, ready
from stone_pipeline.stages import emit, images

A = "a" * 64
BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"


def _row(key, standard_photo, qty="3"):
    return CanonicalRow(src_site="sentas", surrogate_key=key, inventory_quantity=qty, standard_photo=standard_photo)


def _rule(monkeypatch, on):
    monkeypatch.setattr(populate, "SETTINGS", dataclasses.replace(
        SETTINGS, images=dataclasses.replace(SETTINGS.images, require_standard_photo=on)))


@pytest.fixture
def rule_on(monkeypatch):
    _rule(monkeypatch, True)


def _stocked(ledger, rows, cfg):
    """populate_inventory on rows whose products exist (the write-through populates products first)."""
    now = now_iso()
    for r in rows:
        ledger.upsert("product", {"sku": emit._sku(r, cfg), "source": "sen", "state": "synced",
                                  "created_at": now, "updated_at": now}, pk=("sku",))
    return populate_inventory(ledger, rows, cfg)


def _stock(ledger, row):
    r = ledger.get("inventory", "sku", emit._sku(row, load_source("sentas")))
    return r["qty"], r["reason"]


def test_without_a_standardised_photo_a_product_is_delisted_and_comes_back_when_it_has_one(tmp_path, rule_on):
    cfg = load_source("sentas")
    rows = [_row(str(i), True) for i in range(9)] + [_row("x", False)]
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, rows, cfg)
        assert _stock(ledger, rows[-1]) == (0, "no_photo") and _stock(ledger, rows[0]) == (3, None)
        _stocked(ledger, rows[:-1] + [_row("x", True)], cfg)
        assert _stock(ledger, rows[-1]) == (3, None)                                # its photo is published: live again


def test_a_run_that_cannot_tell_leaves_a_hidden_product_hidden_and_a_live_one_live(tmp_path, rule_on):
    cfg = load_source("sentas")
    rows = [_row(str(i), True) for i in range(9)] + [_row("x", False)]
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, rows, cfg)
        _stocked(ledger, [_row(str(i), None) for i in range(9)] + [_row("x", None)], cfg)   # stock-only refresh
        assert _stock(ledger, rows[-1]) == (0, "no_photo") and _stock(ledger, rows[0]) == (3, None)


def test_the_rule_off_changes_nothing(tmp_path, monkeypatch):
    _rule(monkeypatch, False)
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("x", False)], load_source("sentas"))
        assert _stock(ledger, _row("x", False)) == (3, None)


def test_hiding_a_product_already_at_stock_0_is_still_served_to_blokport(tmp_path, rule_on):
    cfg = load_source("sentas")
    sold = _row("x", True, qty="0")
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [sold], cfg)
        ledger.execute("UPDATE inventory SET last_synced_qty = 0")                    # its stock 0 was acked
        assert ready(ledger, "inventory") == []
        _stocked(ledger, [_row("x", False, qty="0")], cfg)
        assert [r["payload"].get("reason") for r in ready(ledger, "inventory")] == ["no_photo"]


def test_a_hidden_product_its_supplier_drops_becomes_plain_dead_stock(tmp_path, rule_on):
    cfg = load_source("sentas")
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("x", False)], cfg)
        sku = emit._sku(_row("x", False), cfg)
        populate_discontinued(ledger, [(sku, "h")])
        assert ledger.get("inventory", "sku", sku)["reason"] is None and purge_discontinued(ledger)["external_ids"] == [sku]


def test_the_dead_stock_purge_keeps_a_hidden_product_and_the_vendor_removal_still_removes_it(tmp_path, rule_on):
    cfg = load_source("sentas")
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("hidden", False), _row("live", True)], cfg)
        populate_discontinued(ledger, [("SEN-GONE", "h")])
        assert purge_discontinued(ledger)["external_ids"] == ["SEN-GONE"]           # dead stock only
        assert purge_discontinued(ledger, reason="vendor_removed")["external_ids"] == [emit._sku(_row("hidden", False), cfg)]


def test_a_photo_hidden_product_always_goes_out_at_stock_0(tmp_path, rule_on):
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [_row("x", False, qty="7")], load_source("sentas"))
        rows = ledger.execute("SELECT qty, reason FROM inventory WHERE reason = 'no_photo'").fetchall()
        assert [tuple(r) for r in rows] == [(0, "no_photo")]


def test_a_bundle_never_redirects_to_a_lot_with_no_piece_on_sale(tmp_path, rule_on):
    cfg = load_source("sentas")
    piece = _row("2555-4", False)
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        now = now_iso()
        ledger.upsert("product", {"sku": "SEN-2555", "source": "sen", "state": "synced", "medusa_id": "m-b",
                                  "created_at": now, "updated_at": now}, pk=("sku",))
        ledger.upsert("inventory", {"sku": "SEN-2555", "qty": 9, "last_synced_qty": 9, "updated_at": now}, pk=("sku",))
        _stocked(ledger, [piece], cfg)
        ledger.execute("UPDATE product SET lot_code = 'L1', medusa_id = 'm-p' WHERE sku = ?", (emit._sku(piece, cfg),))
        now = now_iso()
        ledger.upsert("product", {"sku": "SEN-2555-1", "source": "sen", "state": "synced", "lot_code": "L1",
                                  "medusa_id": "m-sold", "created_at": now, "updated_at": now}, pk=("sku",))
        ledger.upsert("inventory", {"sku": "SEN-2555-1", "qty": 0, "last_synced_qty": 0, "updated_at": now}, pk=("sku",))
        populate_discontinued(ledger, [("SEN-2555", "h")], replaced={"SEN-2555": "L1"})
        assert "SEN-2555" not in [r["external_id"] for r in ready(ledger, "inventory")]   # one hidden, one sold
        _stocked(ledger, [_row("2555-4", True)], cfg)                                   # its photo is published
        assert "SEN-2555" in [r["external_id"] for r in ready(ledger, "inventory")]


def _improved(sha):
    return f"{BASE}/improved/sentas/{sha}.png"


def test_the_image_stage_says_whether_each_product_has_a_standardised_photo_or_that_it_cannot_tell(monkeypatch):
    monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
    rows = [CanonicalRow(src_site="sentas", surrogate_key=k, format_value="Slab", image_keys=[_improved(s)])
            for k, s in (("1", A), ("2", "b" * 64))]
    cfg = type("Cfg", (), {"mode": "passthrough"})()
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: {"main": {A: "e"}, "white": {}, "studio": {}, "close": {}})
    monkeypatch.setattr(images, "_load_owner_removed", lambda cfg: set())
    monkeypatch.setattr(images, "_load_assigned", lambda cfg: {})
    images.run(rows, cfg=cfg)
    images.link_standard(rows, rows, cfg=cfg)
    assert [r.standard_photo for r in rows] == [True, False]
    for pool in (None, {}, {"main": {}, "white": {}}):                    # unreadable; local mode; nothing published
        rows = [CanonicalRow(src_site="sentas", surrogate_key="3", format_value="Slab", image_keys=[_improved(A)])]
        monkeypatch.setattr(images, "_load_standard_views", lambda cfg, pool=pool: pool)
        images.run(rows, cfg=cfg)
        images.link_standard(rows, rows, cfg=cfg)
        assert rows[0].standard_photo is None and rows[0].thumbnail_key == _improved(A)


_VIEWS = {"main": {A: "e"}, "white": {A: "w"}, "studio": {A: "s"}, "close": {}, "cutout": {A: "c+j"}}


def _stage(monkeypatch, rows, removed=frozenset(), assigned=None, emitted=None):
    """The image stage (7) on the rows, then the standardised linking once validation has said which are emitted (all,
    unless `emitted` names them): the conflict set it classified."""
    monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: _VIEWS)
    monkeypatch.setattr(images, "_load_owner_removed", lambda cfg: removed)
    monkeypatch.setattr(images, "_load_assigned", lambda cfg: {} if assigned is None else assigned)
    cfg = type("Cfg", (), {"mode": "passthrough"})()
    images.run(rows, cfg=cfg)
    return images.link_standard(rows, rows if emitted is None else emitted, cfg=cfg)


def _slabs(*keys, variety="v1", finish="f1"):
    return [CanonicalRow(src_site="sentas", surrogate_key=k, format_value="Slab", image_keys=[_improved(A)],
                         variation_id=variety, finish_id=finish, handle=f"{k}-sen-{k}") for k in keys]


def _sku(row):
    return emit._sku(row, load_source("sentas"))


@pytest.mark.parametrize("on", [True, False])
def test_a_removed_product_gets_nothing_standardised_even_from_a_sibling_s_published_photo(monkeypatch, tmp_path, on):
    # its photo is published for a live sibling of the same stone: the removed product links none of it (no standardised
    # thumbnail, gallery, 3D or version) whatever require_standard_photo says, and the pool being known, it is "known, no
    # standardised photo" (hidden with the rule on), never "cannot tell"; the sibling is unaffected
    _rule(monkeypatch, on)
    live, removed = rows = _slabs("live", "removed")
    _stage(monkeypatch, rows, removed={_sku(rows[1])})
    assert live.standard_photo is True and live.thumbnail_key.endswith(f"/standard/main/sentas/{A}.jpg")
    assert live.model_3d_key is not None and live.standard_version is not None
    assert removed.standard_photo is False and removed.thumbnail_key == _improved(A)
    assert removed.model_3d_key is None and removed.standard_version is None
    assert removed.product_image_keys == [_improved(A)]
    live.inventory_quantity = removed.inventory_quantity = "3"
    with Ledger.open(tmp_path / "l.db", env="development") as ledger:
        _stocked(ledger, [live, removed], load_source("sentas"))
        assert _stock(ledger, live) == (3, None)
        assert _stock(ledger, removed) == ((0, "no_photo") if on else (3, None))


def test_a_published_photo_products_of_several_stones_show_is_linked_only_to_the_one_assigned(monkeypatch):
    # classified from this produce's own rows, the way the photo process classifies the sheet (index.shared_photos), so
    # a conflict the produce has just created, or an assignment file not written yet, links the photo to nobody
    mine, partner = _slabs("mine", variety="v1") + _slabs("partner", variety="v2")
    _stage(monkeypatch, [mine, partner], assigned={("sentas", A): {_sku(mine)}})
    assert [mine.standard_photo, partner.standard_photo] == [True, False]
    assert mine.thumbnail_key.endswith(f"/standard/main/sentas/{A}.jpg") and mine.model_3d_key
    assert partner.thumbnail_key == _improved(A) and partner.model_3d_key is None and partner.standard_version is None
    mine, partner = _slabs("mine", variety="v1") + _slabs("partner", finish="f2")
    _stage(monkeypatch, [mine, partner], assigned={})                              # no assignment (yet): nobody
    assert [mine.standard_photo, partner.standard_photo] == [False, False]


def test_a_photo_a_live_product_shows_besides_its_lead_still_counts_as_shown(monkeypatch):
    # the sheet carries only a live product's lead photo; the produce sees every photo a product has, so a photo that
    # another stone's product also shows is a conflict here even when the sheet could not tell (stricter, never looser)
    live = CanonicalRow(src_site="sentas", surrogate_key="live", format_value="Slab", variation_id="v1", finish_id="f1",
                        handle="live-sen-live", image_keys=[_improved("b" * 64), _improved(A)])
    other = _slabs("other", variety="v2")[0]
    _stage(monkeypatch, [live, other])
    assert other.standard_photo is False and other.thumbnail_key == _improved(A)


def test_products_of_one_stone_share_a_published_photo_and_an_unbound_row_makes_no_conflict(monkeypatch):
    rows = _slabs("block-1", "block-2") + [CanonicalRow(src_site="sentas", surrogate_key="held", format_value="Slab",
                                                        image_keys=[_improved(A)])]
    _stage(monkeypatch, rows, emitted=rows[:2])                                  # validation rejects the unbound row
    assert [r.standard_photo for r in rows[:2]] == [True, True]


def test_a_run_that_cannot_read_the_removals_or_the_assignments_cannot_tell(monkeypatch):
    for removed, assigned in ((None, {}), (set(), None)):
        monkeypatch.setattr(images, "_load_assigned", lambda cfg, a=assigned: a)
        rows = _slabs("1")
        monkeypatch.setattr(images, "_link_from_manifest", lambda rows, cfg, stats: None)
        monkeypatch.setattr(images, "_load_standard_views", lambda cfg: _VIEWS)
        monkeypatch.setattr(images, "_load_owner_removed", lambda cfg, r=removed: r)
        cfg = type("Cfg", (), {"mode": "passthrough"})()
        images.run(rows, cfg=cfg)
        images.link_standard(rows, rows, cfg=cfg)
        assert rows[0].standard_photo is None


def test_the_owner_s_removals_and_the_assignments_are_read_from_their_stores(monkeypatch):
    from stone_pipeline.io import imagestore
    files = {imagestore.owner_removed_key("SEN-1"): b"{}", imagestore.owner_removed_key("MAR-P/02/16"): b"{}",
             imagestore.assigned_key("sentas", A): b'["SEN-1"]'}
    monkeypatch.setattr(images, "_list_objects", lambda p: {k: "e" for k in files if k.startswith(p)})
    monkeypatch.setattr(images, "_get_object", lambda key: files[key])
    s3cfg, local = type("Cfg", (), {"mode": "s3"})(), type("Cfg", (), {"mode": "local"})()
    assert images._load_owner_removed(s3cfg) == {"SEN-1", "MAR-P/02/16"} and images._load_owner_removed(local) == set()
    assert images._load_assigned(s3cfg) == {("sentas", A): {"SEN-1"}} and images._load_assigned(local) == {}


def test_only_a_storage_error_reading_them_is_cannot_tell_a_bug_fails_loud(monkeypatch):
    from botocore.exceptions import EndpointConnectionError
    s3cfg = type("Cfg", (), {"mode": "s3"})()
    monkeypatch.setattr(images, "_list_objects", lambda p: (_ for _ in ()).throw(EndpointConnectionError(endpoint_url="s3")))
    assert images._load_owner_removed(s3cfg) is None and images._load_assigned(s3cfg) is None
    monkeypatch.setattr(images, "_list_objects", lambda p: (_ for _ in ()).throw(TypeError("a bug")))
    for load in (images._load_owner_removed, images._load_assigned):
        with pytest.raises(TypeError):
            load(s3cfg)


def test_a_product_validation_rejects_never_makes_a_conflict_for_a_live_one(monkeypatch):
    # an off-sheet product (bound to a variety, then rejected) shares the live one's published photo: it is not a listing,
    # so the photo is no conflict and the live product keeps it (the process, the page and the produce see one set)
    live, off_sheet = _slabs("live", variety="calacatta-viola") + _slabs("off-sheet", variety="calacatta-green")
    conflicts = _stage(monkeypatch, [live, off_sheet], emitted=[live])
    assert conflicts == {"sentas": {}}
    assert live.standard_photo is True and live.thumbnail_key.endswith(f"/standard/main/sentas/{A}.jpg")


def test_the_conflict_set_is_classified_over_the_emitted_products_once_per_source(monkeypatch):
    mine, partner = _slabs("mine", variety="v1") + _slabs("partner", variety="v2")
    assert _stage(monkeypatch, [mine, partner]) == {"sentas": {A: ["mine-sen-mine", "partner-sen-partner"]}}
    monkeypatch.setattr(images, "_load_standard_views", lambda cfg: None)                # the pool unreadable
    assert images.link_standard([mine, partner], [mine, partner], cfg=type("Cfg", (), {"mode": "passthrough"})()) is None


def _writes(monkeypatch, mode="s3", dry_run=False, stored=None):
    """save_conflicts against a bucket in a dict: (the keys written, the bucket)."""
    stored = {} if stored is None else stored
    written = []
    monkeypatch.setattr(images, "SETTINGS", dataclasses.replace(
        SETTINGS, s3=dataclasses.replace(SETTINGS.s3, dry_run=dry_run), images=dataclasses.replace(SETTINGS.images, mode=mode)))
    monkeypatch.setattr(images, "_put_object", lambda key, body: written.append(key) or stored.update({key: body}))
    monkeypatch.setattr(images, "_stored_object", lambda key: stored.get(key))
    return written, stored


def test_the_conflict_set_is_stored_per_source_for_the_photo_process_and_the_review_page(monkeypatch):
    from stone_pipeline.io import imagestore
    written, stored = _writes(monkeypatch)
    images.save_conflicts({"sentas": {A: ["mine-sen-mine", "partner-sen-partner"]}, "zucchi": {}})
    assert {k: json.loads(v) for k, v in stored.items()} == {
        imagestore.conflicts_key("sentas"): {A: ["mine-sen-mine", "partner-sen-partner"]}, imagestore.conflicts_key("zucchi"): {}}
    written.clear()
    images.save_conflicts({"sentas": {A: ["mine-sen-mine", "partner-sen-partner"]}, "zucchi": {}})
    assert written == []                                      # unchanged: no new version in the versioned bucket


@pytest.mark.parametrize("mode, dry_run", [("local", False), ("passthrough", False), ("s3", True)])
def test_a_run_that_does_not_write_the_bucket_never_stores_the_conflict_set(monkeypatch, caplog, mode, dry_run):
    # a laptop run (local or passthrough images, or the s3 dry run) must never overwrite an environment's set
    written, _ = _writes(monkeypatch, mode=mode, dry_run=dry_run)
    images.save_conflicts({"sentas": {A: ["mine-sen-mine", "partner-sen-partner"]}})
    assert written == [] and "conflict set not stored" in caplog.text


def test_a_source_with_no_hosted_photo_has_an_empty_set_so_missing_means_not_produced_yet(monkeypatch):
    rows = [CanonicalRow(src_site="sentas", surrogate_key="1", format_value="Slab", handle="h1", variation_id="v1",
                         image_keys=["https://supplier.example/x.jpg"])]
    assert _stage(monkeypatch, rows) == {"sentas": {}}
