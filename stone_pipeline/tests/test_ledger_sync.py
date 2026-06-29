"""Sync service over the ledger: status, and the serve/ack loop that keeps the
ledger and Medusa in sync."""

from __future__ import annotations

from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.sync import ack, ready, status


def _variation(ledger, key, state, medusa_id=None, type_="Marble", image_url="https://s3/tex.png"):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "branch": "slab", "type": type_, "name": "x",
                                "aliases": "[]", "image_url": image_url, "image_sha256": None,
                                "image_model": None, "volume": "", "medusa_id": medusa_id,
                                "in_full": 1, "payload_hash": "", "state": state,
                                "first_seen": now, "last_synced": None,
                                "created_at": now, "updated_at": now}, pk=("key",))


def _product(ledger, sku, variation_key, state):
    now = now_iso()
    ledger.upsert("product", {"sku": sku, "source": "s", "variation_key": variation_key,
                              "state": state, "created_at": now, "updated_at": now}, pk=("sku",))


def _seed(ledger: Ledger) -> None:
    now = now_iso()
    ledger.upsert("attribute", {"category": "color", "value": "Black", "medusa_id": "C1",
                                "state": "synced", "created_at": now, "updated_at": now},
                  pk=("category", "value"))
    for key, state in [("slab_a_1", "synced"), ("slab_b_2", "pending")]:
        ledger.upsert("variation", {"key": key, "branch": "slab", "type": "", "name": "x",
                                    "aliases": "[]", "image_url": "", "image_sha256": None,
                                    "image_model": None, "volume": "", "medusa_id": None,
                                    "in_full": 1, "payload_hash": "", "state": state,
                                    "first_seen": now, "last_synced": None,
                                    "created_at": now, "updated_at": now}, pk=("key",))
    ledger.upsert("product", {"sku": "S-1", "source": "s", "variation_key": "slab_a_1",
                              "state": "pending", "created_at": now, "updated_at": now}, pk=("sku",))
    # one stock row that moved (delta) and one already synced
    ledger.upsert("inventory", {"sku": "S-1", "qty": 5, "last_synced_qty": None,
                                "updated_at": now}, pk=("sku",))


def test_status_reports_state_histograms_and_inventory_delta(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed(ledger)
        st = status(ledger)

    assert st["attribute"] == {"synced": 1}
    assert st["variation"] == {"synced": 1, "pending": 1}
    assert st["product"] == {"pending": 1}
    assert st["combination"] == {}              # empty until materialized
    assert st["inventory"] == {"total": 1, "delta": 1}


def test_sync_loop_serve_ack_converges(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_new_1", state="pending")          # new, no id yet
        _product(ledger, "S-1", "slab_new_1", state="pending")

        # the variation is served; the product is NOT (its variation is not synced)
        assert [r["external_id"] for r in ready(ledger, "variations")] == ["slab_new_1"]
        assert ready(ledger, "products") == []

        # Medusa applies the variation and acks the id it minted -> it stops being served
        ack(ledger, "variations", "slab_new_1", "VID-1", "created")
        assert ready(ledger, "variations") == []
        v = ledger.get("variation", "key", "slab_new_1")
        assert v["medusa_id"] == "VID-1" and v["state"] == "synced"

        # now the product is eligible (its variation is synced); apply + ack it
        assert [r["external_id"] for r in ready(ledger, "products")] == ["S-1"]
        ack(ledger, "products", "S-1", "PID-1", "created")
        assert ready(ledger, "products") == []
        p = ledger.get("product", "sku", "S-1")
        assert p["medusa_id"] == "PID-1" and p["state"] == "synced"

        # a failed ack returns it to dirty, so it is offered again next pull
        ack(ledger, "products", "S-1", None, "failed")
        assert [r["external_id"] for r in ready(ledger, "products")] == ["S-1"]


def test_dispatch_routes_status_ready_ack(tmp_path):
    from stone_pipeline.ledger.server import dispatch

    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_new_1", state="pending")

        code, body = dispatch(ledger, "GET", "status", {}, None)
        assert code == 200 and body["variation"] == {"pending": 1}

        code, body = dispatch(ledger, "GET", "variations", {}, None)
        assert code == 200 and [i["external_id"] for i in body["items"]] == ["slab_new_1"]

        code, body = dispatch(ledger, "POST", "ack", {},
                              [{"type": "variations", "external_id": "slab_new_1",
                                "medusa_id": "VID-1", "status": "created"}])
        assert code == 200 and body["acked"] == 1
        assert ledger.get("variation", "key", "slab_new_1")["state"] == "synced"

        assert dispatch(ledger, "GET", "bogus", {}, None)[0] == 404
        assert dispatch(ledger, "POST", "ack", {}, {"not": "a list"})[0] == 400


def test_untyped_variation_is_held_not_served(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_typed_1", state="pending", type_="Granite")
        _variation(ledger, "slab_untyped_2", state="pending", type_="")  # no canonical type
        served = [r["external_id"] for r in ready(ledger, "variations")]
        assert served == ["slab_typed_1"], "an untyped variation must be held, not served broken"


def test_fill_variation_types_from_key(tmp_path):
    from stone_pipeline.ledger.populate import fill_variation_types

    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        for cat, val in [("type", "Marble"), ("type", "Granite")]:
            ledger.upsert("attribute", {"category": cat, "value": val, "medusa_id": "x",
                                        "state": "synced", "created_at": now, "updated_at": now},
                          pk=("category", "value"))
        _variation(ledger, "block_marble_carrara_uuid", state="pending", type_="")
        _variation(ledger, "slab_granite_kashmir_white_uuid", state="pending", type_="")
        assert fill_variation_types(ledger) == 2
        assert ledger.get("variation", "key", "block_marble_carrara_uuid")["type"] == "Marble"
        assert ledger.get("variation", "key", "slab_granite_kashmir_white_uuid")["type"] == "Granite"


def test_inventory_sync_serves_delta_for_synced_products_only(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")
        _product(ledger, "P-1", "slab_v1", state="synced")    # in Medusa -> stock can sync
        _product(ledger, "P-2", "slab_v1", state="pending")   # not yet -> stock held
        for sku in ("P-1", "P-2"):
            ledger.upsert("inventory", {"sku": sku, "qty": 7, "last_synced_qty": None,
                                        "updated_at": now}, pk=("sku",))

        assert [r["external_id"] for r in ready(ledger, "inventory")] == ["P-1"]
        ack(ledger, "inventory", "P-1")            # Medusa now holds qty 7
        assert ready(ledger, "inventory") == []    # no longer a delta
        inv = ledger.get("inventory", "sku", "P-1")
        assert inv["last_synced_qty"] == 7 and inv["qty"] == 7


def test_product_held_until_variation_has_texture(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        # variation synced but with NO live texture -> its product is held (H2 decision)
        _variation(ledger, "slab_notex", state="synced", medusa_id="V1", image_url="")
        _product(ledger, "P-1", "slab_notex", state="pending")
        assert ready(ledger, "products") == [], "product must be held until its texture is live"
        # the texture lands -> the product becomes eligible
        ledger.execute("UPDATE variation SET image_url = 'https://s3/tex.png' WHERE key = 'slab_notex'")
        assert [r["external_id"] for r in ready(ledger, "products")] == ["P-1"]


def test_product_payload_carries_no_medusa_ids(tmp_path):
    # the review's red flag: the scraper's payload must hold ZERO Medusa ids, only
    # external references Medusa resolves (vendor, names), symmetric with color/type.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")   # synced + live texture
        ledger.upsert("product", {"sku": "P-1", "source": "polonine", "variation_key": "slab_v1",
                                  "color": "Black", "state": "pending",
                                  "created_at": now, "updated_at": now}, pk=("sku",))
        items = ready(ledger, "products")
        assert len(items) == 1
        payload = items[0]["payload"]
        for forbidden in ("company_id", "sales_channel_id", "ports"):
            assert forbidden not in payload, f"{forbidden} is a Medusa id and must not be in the payload"
        assert payload["vendor"] == "polonine"       # the external reference Medusa resolves
        assert "origin_country_code" in payload      # Medusa derives ports from this


def test_glue_full_sync_converges(tmp_path):
    # the capstone: drive the whole loop the way Medusa's pull job would, and assert
    # it converges with everything synced and the variation-before-product order held.
    from stone_pipeline.ledger.simulate import simulate_sync

    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        _variation(ledger, "slab_v1", state="pending", type_="Marble")
        _variation(ledger, "slab_v2", state="pending", type_="Granite")
        _product(ledger, "P-1", "slab_v1", state="pending")
        _product(ledger, "P-2", "slab_v2", state="pending")
        ledger.upsert("inventory", {"sku": "P-1", "qty": 7, "last_synced_qty": None,
                                    "updated_at": now}, pk=("sku",))   # a stock delta

        report = simulate_sync(ledger)

        assert report["converged"] is True
        assert report["applied"] == {"variations": 2, "products": 2, "inventory": 1}
        st = status(ledger)
        assert st["variation"].get("pending", 0) == 0
        assert st["product"].get("pending", 0) == 0
        assert ledger.get("product", "sku", "P-1")["state"] == "synced"
        assert ledger.get("product", "sku", "P-1")["medusa_id"] == "SIM-PRO-P-1"
        assert ledger.get("inventory", "sku", "P-1")["last_synced_qty"] == 7   # stock synced
        # nothing left to serve in any lane
        assert all(ready(ledger, t) == [] for t in ("variations", "products", "inventory"))
