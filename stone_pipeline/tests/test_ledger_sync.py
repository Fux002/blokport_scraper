"""Sync service over the ledger: status, and the serve/ack loop that keeps the
ledger and Medusa in sync."""

from __future__ import annotations

import json

import pytest

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


def test_reset_sync_state_clean_start(tmp_path):
    # Medusa was wiped -> the ledger must follow: every entity back to 'pending', all Medusa ids +
    # sync bookkeeping dropped, but the scraped CONTENT (name/type/image) untouched.
    from stone_pipeline.ledger.sync import reset_sync_state
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="OLD-VID")
        _product(ledger, "P-1", "slab_v1", state="synced")
        ledger.execute("UPDATE product SET medusa_id='OLD-PID', last_synced='x' WHERE sku='P-1'")
        ledger.execute("UPDATE variation SET last_synced='x' WHERE key='slab_v1'")
        ledger.upsert("inventory", {"sku": "P-1", "qty": 7, "last_synced_qty": 7,
                                    "updated_at": now_iso()}, pk=("sku",))

        out = reset_sync_state(ledger)
        assert out["variation"] >= 1 and out["product"] >= 1 and out["inventory"] >= 1

        v = ledger.get("variation", "key", "slab_v1")
        p = ledger.get("product", "sku", "P-1")
        assert v["state"] == "pending" and v["medusa_id"] is None and v["last_synced"] is None
        assert p["state"] == "pending" and p["medusa_id"] is None and p["last_synced"] is None
        assert ledger.get("inventory", "sku", "P-1")["last_synced_qty"] is None   # stock re-serves
        # CONTENT preserved -- only the sync overlay was cleared
        assert v["type"] == "Marble" and v["image_url"] == "https://s3/tex.png"
        # and it is now servable again from scratch
        assert [r["external_id"] for r in ready(ledger, "variations")] == ["slab_v1"]


def test_reset_hard_drops_scraper_output_but_keeps_base_variations(tmp_path):
    # hard reset wipes the SCRAPER output (products + stock) so a re-scrape rebuilds it, but the
    # variation/backbone rows (the base config) are NEVER deleted -- only their sync overlay clears.
    from stone_pipeline.ledger.sync import reset_sync_state
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="VID")
        _product(ledger, "P-1", "slab_v1", state="synced")
        ledger.upsert("inventory", {"sku": "P-1", "qty": 5, "last_synced_qty": 5,
                                    "updated_at": now_iso()}, pk=("sku",))
        out = reset_sync_state(ledger, hard=True)
        assert ledger.get("product", "sku", "P-1") is None            # scraper output deleted
        assert ledger.get("inventory", "sku", "P-1") is None
        v = ledger.get("variation", "key", "slab_v1")                 # base config KEPT
        assert v is not None and v["state"] == "pending" and v["medusa_id"] is None
        assert v["type"] == "Marble" and v["image_url"] == "https://s3/tex.png"   # content intact
        assert out["product"] == 1 and out["inventory"] == 1


def test_reset_scoped_to_one_source_leaves_the_others(tmp_path):
    # a per-source reset touches only that source's products; the shared base layer is left alone.
    from stone_pipeline.ledger.sync import reset_sync_state
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="VID")
        now = now_iso()
        for sku, src in (("A-1", "aaa"), ("B-1", "bbb")):
            ledger.upsert("product", {"sku": sku, "source": src, "variation_key": "slab_v1",
                                      "state": "synced", "created_at": now, "updated_at": now}, pk=("sku",))
        reset_sync_state(ledger, source_codes=["aaa"], hard=True)
        assert ledger.get("product", "sku", "A-1") is None            # aaa dropped
        assert ledger.get("product", "sku", "B-1") is not None        # bbb untouched
        assert ledger.get("variation", "key", "slab_v1")["state"] == "synced"   # shared layer untouched


def test_purge_discontinued_deletes_only_qty0_products(tmp_path):
    # dead stock (qty 0) is hard-deleted so the graveyard stops growing; in-stock products stay; the
    # returned external_ids are exactly what Medusa deletes (the ②③ half). A purged sku, having no
    # prior ledger row, recreates as NEW on reappearance (the prev-is-None rule, covered by cold_start).
    from stone_pipeline.ledger.sync import purge_discontinued
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")
        for sku, src, qty in (("POL-1", "pol", 0), ("POL-2", "pol", 5), ("ZUC-1", "zuc", 0)):
            ledger.upsert("product", {"sku": sku, "source": src, "variation_key": "slab_v1",
                                      "medusa_id": "M-" + sku, "state": "synced",
                                      "created_at": now, "updated_at": now}, pk=("sku",))
            ledger.upsert("inventory", {"sku": sku, "qty": qty, "last_synced_qty": qty,
                                        "updated_at": now}, pk=("sku",))

        out = purge_discontinued(ledger)
        assert set(out["external_ids"]) == {"POL-1", "ZUC-1"} and out["product"] == 2   # both qty-0 sources
        assert ledger.get("product", "sku", "POL-1") is None and ledger.get("inventory", "sku", "POL-1") is None
        assert ledger.get("product", "sku", "POL-2") is not None                       # in-stock kept

        # scoped purge touches only that source's dead stock
        for sku in ("POL-1", "ZUC-1"):   # re-seed both qty-0 again
            ledger.upsert("product", {"sku": sku, "source": sku[:3].lower(), "variation_key": "slab_v1",
                                      "state": "synced", "created_at": now, "updated_at": now}, pk=("sku",))
            ledger.upsert("inventory", {"sku": sku, "qty": 0, "last_synced_qty": 0, "updated_at": now}, pk=("sku",))
        out = purge_discontinued(ledger, source_codes=["pol"])
        assert out["external_ids"] == ["POL-1"]                  # zuc untouched by a pol-scoped purge
        assert ledger.get("product", "sku", "ZUC-1") is not None


def test_serve_isolates_a_row_with_corrupt_json(tmp_path):
    # one row with an unserializable cell must NOT 500 the whole leased page: it is dead-lettered
    # (gap_held) and dropped, the good rows still serve.
    from stone_pipeline.ledger.sync import ready_variations
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_good", state="pending")
        _variation(ledger, "slab_bad", state="pending")
        ledger.execute("UPDATE variation SET aliases = '{corrupt' WHERE key = 'slab_bad'")
        served = ready_variations(ledger)                       # must NOT raise
        keys = {v["external_id"] for v in served}
        assert "slab_good" in keys and "slab_bad" not in keys   # good served, bad dropped
        assert ledger.get("variation", "key", "slab_bad")["state"] == "gap_held"    # dead-lettered
        assert ledger.get("variation", "key", "slab_good")["state"] == "syncing"    # leased normally


def test_serve_in_flight_detects_a_lease(tmp_path):
    from stone_pipeline.ledger.sync import serve_in_flight, ready
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="pending")
        assert serve_in_flight(ledger) is False        # nothing leased
        ready(ledger, "variations")                    # leases slab_v1 -> 'syncing'
        assert serve_in_flight(ledger) is True          # a pull is in flight


def test_reset_refuses_atomically_while_a_lease_is_held(tmp_path):
    # the real cross-process guard: reset_sync_state itself refuses (raises) if a pull holds a lease,
    # not just the caller -- so the CLI path is guarded too, and there's no check-then-act window.
    from stone_pipeline.ledger.sync import reset_sync_state, ServeInFlight, ready
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="pending")
        _variation(ledger, "slab_v2", state="synced", medusa_id="V2")
        ready(ledger, "variations")                    # leases the pending one -> 'syncing'
        with pytest.raises(ServeInFlight):
            reset_sync_state(ledger)
        # nothing was reset -- the synced one keeps its id (the canary write rolled back on raise)
        assert ledger.get("variation", "key", "slab_v2")["medusa_id"] == "V2"


def test_serving_leases_so_overlapping_pulls_never_double_serve(tmp_path):
    # D5 in-flight guard: a pull LEASES its rows to 'syncing'. A second, overlapping pull (Medusa's
    # job paginating, or two triggers) must get NOTHING for those rows -- never the same entity twice.
    from stone_pipeline.ledger.sync import count_ready, reap_stale_syncing
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")
        for i in range(3):
            _product(ledger, f"P-{i}", "slab_v1", state="pending")

        assert count_ready(ledger, "products") == 3            # peek: 3 eligible, NOT leased
        first = {i["external_id"] for i in ready(ledger, "products")}   # pull 1 leases all three
        assert first == {"P-0", "P-1", "P-2"}
        assert ready(ledger, "products") == []                 # pull 2 (overlapping): nothing to re-serve
        assert count_ready(ledger, "products") == 0            # all in-flight
        assert ledger.counts("product") == {"syncing": 3}

        # a crashed puller never acks -> the lease must be reclaimed so the rows are not lost forever.
        assert reap_stale_syncing(ledger) == 0                 # nothing stale yet (fresh lease)
        ledger.execute("UPDATE product SET updated_at = '2000-01-01T00:00:00+00:00'")   # time passes
        assert reap_stale_syncing(ledger) == 3                 # leases expire -> back to 'dirty'
        assert {i["external_id"] for i in ready(ledger, "products")} == {"P-0", "P-1", "P-2"}   # re-served


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


def test_fill_variation_types_hyphenated_multiword(tmp_path):
    # regression: a hyphenated multi-word type (Semi-Precious Stone) must type from a Key slug
    # that uses underscores (slab_semi_precious_stone_...). The vocab hyphen vs the slug
    # underscore/space mismatch previously left every such variation untyped -> held forever.
    from stone_pipeline.ledger.populate import fill_variation_types

    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        ledger.upsert("attribute", {"category": "type", "value": "Semi-Precious Stone",
                                    "medusa_id": "x", "state": "synced",
                                    "created_at": now, "updated_at": now}, pk=("category", "value"))
        _variation(ledger, "slab_semi_precious_stone_agata_brown_uuid", state="pending", type_="")
        assert fill_variation_types(ledger) == 1
        assert ledger.get("variation", "key",
                          "slab_semi_precious_stone_agata_brown_uuid")["type"] == "Semi-Precious Stone"


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


def test_failed_ack_dead_letters_after_cap_then_requeues(tmp_path):
    # error handling when Medusa keeps rejecting an entity: it must NOT re-serve forever. After the
    # attempt cap it dead-letters (gap_held) -> stops serving, stores the reason, surfaces in status
    # + failures(); an explicit requeue (or a success) recovers it.
    from stone_pipeline.ledger.sync import (ack, ready, requeue_dead_lettered, failures, status,
                                            _MAX_SYNC_ATTEMPTS)
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")
        _product(ledger, "P-1", "slab_v1", state="pending")
        assert [i["external_id"] for i in ready(ledger, "products")] == ["P-1"]
        for _ in range(_MAX_SYNC_ATTEMPTS):
            ack(ledger, "products", "P-1", status_="failed", reason="Medusa 400: bad company")
        row = ledger.get("product", "sku", "P-1")
        assert row["state"] == "gap_held" and row["sync_attempts"] == _MAX_SYNC_ATTEMPTS
        assert "bad company" in row["sync_error"]
        assert ready(ledger, "products") == []                       # no infinite poison-pill loop
        assert status(ledger)["product"].get("gap_held") == 1        # visible in status
        assert failures(ledger)[0]["external_id"] == "P-1"           # drill-down: WHY
        assert requeue_dead_lettered(ledger) == 1                    # recovery
        assert [i["external_id"] for i in ready(ledger, "products")] == ["P-1"]
        ack(ledger, "products", "P-1", medusa_id="M1", status_="synced")   # a success clears it
        r = ledger.get("product", "sku", "P-1")
        assert r["state"] == "synced" and r["sync_attempts"] == 0 and r["sync_error"] is None


def test_synced_ack_does_not_unretire_a_retiring_variation(tmp_path):
    # a stray/out-of-order synced ack must NOT flip a RETIRING variation back to synced -- its tombstone
    # still serves /removed, so un-retiring it would diverge the two systems.
    from stone_pipeline.ledger.sync import ack
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="retiring", medusa_id="V1")
        assert ack(ledger, "variations", "slab_v1", medusa_id="V1", status_="synced") == 0   # refused
        assert ledger.get("variation", "key", "slab_v1")["state"] == "retiring"               # still retiring


def test_ack_batch_isolates_a_bad_ack(tmp_path):
    # one malformed ack in a batch must NOT drop the whole batch: it's skipped+logged, the rest apply.
    from stone_pipeline.ledger.sync import ack_batch
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")
        _product(ledger, "P-1", "slab_v1", state="pending")
        res = ack_batch(ledger, [{"type": "products", "external_id": "P-1", "medusa_id": "M1",
                                  "status": "synced"}, {"malformed": True}])
        assert res == {"applied": 1, "missed": 0, "skipped": 1}
        assert ledger.get("product", "sku", "P-1")["state"] == "synced"   # the good ack still applied


def test_ack_refuses_product_ahead_of_its_variation(tmp_path):
    # F2 eligibility guard: a duplicate/out-of-order ack must NOT mark a product synced before its
    # variation is synced (which would then let its inventory load for a variety Medusa may not hold).
    from stone_pipeline.ledger.sync import ack
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="pending", medusa_id="V1")    # variation NOT synced yet
        _product(ledger, "P-1", "slab_v1", state="pending")
        assert ack(ledger, "products", "P-1", medusa_id="M1", status_="synced") == 0   # refused (0 rows)
        assert ledger.get("product", "sku", "P-1")["state"] == "pending"               # not marked synced
        ledger.execute("UPDATE variation SET state='synced' WHERE key='slab_v1'")      # variation catches up
        assert ack(ledger, "products", "P-1", medusa_id="M1", status_="synced") == 1   # now applies
        assert ledger.get("product", "sku", "P-1")["state"] == "synced"


def test_product_payload_carries_thumbnail_and_all_image_lanes(tmp_path):
    # the pull must send the SAME image set the working CSV import did: thumbnail (main image) +
    # oriented (Front/Right/Back/Left) + gallery. Missing the thumbnail = a product with no main image.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")   # synced + typed + textured
        ledger.upsert("product", {"sku": "P-1", "source": "pol", "variation_key": "slab_v1",
                                  "state": "pending", "thumbnail_key": "https://s3/thumb.jpg",
                                  "oriented_image_keys": json.dumps(["https://s3/front.png", "https://s3/right.png"]),
                                  "product_image_keys": json.dumps(["https://s3/g1.jpg", "https://s3/g2.jpg"]),
                                  "created_at": now, "updated_at": now}, pk=("sku",))
        payload = ready(ledger, "products")[0]["payload"]
        assert payload["thumbnail"] == "https://s3/thumb.jpg"
        assert payload["oriented_images"] == ["https://s3/front.png", "https://s3/right.png"]
        assert payload["image_urls"] == ["https://s3/g1.jpg", "https://s3/g2.jpg"]


def test_product_held_until_variation_has_texture(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        # variation synced but with NO live texture -> its product is held (H2 decision)
        _variation(ledger, "slab_notex", state="synced", medusa_id="V1", image_url="")
        _product(ledger, "P-1", "slab_notex", state="pending")
        assert ready(ledger, "products") == [], "product must be held until its texture is live"
        # the texture lands -> the product becomes eligible
        ledger.execute("UPDATE variation SET image_url = 'https://s3/tex.png' WHERE key = 'slab_notex'")
        assert [r["external_id"] for r in ready(ledger, "products")] == ["P-1"]


def test_product_payload_carries_only_the_company_id(tmp_path):
    # The payload holds ZERO high-cardinality Medusa ids (variation/attribute/channel), only
    # external references Medusa resolves. company_id is the ONE deliberate exception: a small,
    # hand-managed, per-source id set in :4200 so Medusa can allocate the seller directly.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        now = now_iso()
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")   # synced + live texture
        ledger.upsert("product", {"sku": "P-1", "source": "pol", "vendor": "Polonine Stone Co",
                                  "company_id": "comp_dev_pol", "variation_key": "slab_v1",
                                  "color": "Black", "type": "Marble", "category": "Slabs",
                                  "state": "pending",
                                  "created_at": now, "updated_at": now}, pk=("sku",))
        items = ready(ledger, "products")
        assert len(items) == 1
        payload = items[0]["payload"]
        for forbidden in ("sales_channel_id", "ports", "variation_id"):
            assert forbidden not in payload, f"{forbidden} is a Medusa id and must not be in the payload"
        # vendor is the agnostic company name; company_id is the per-source Medusa seller id (:4200)
        assert payload["vendor"] == "Polonine Stone Co"
        assert payload["company_id"] == "comp_dev_pol"
        assert "origin_country_code" in payload      # Medusa derives ports from this
        # type + category are denormalized display copies of the variation's identity (name, for
        # metadata.type_name display); resolve identity from variation_external_id, not these.
        assert payload["type"] == "Marble" and payload["category"] == "Slabs"


def test_product_held_when_variation_synced_but_untyped(tmp_path):
    # the product inherits category+type from its variation, so a synced-but-untyped
    # variation (e.g. a bootstrap-synced row fill_variation_types could not resolve) must
    # not let its product list with an empty type.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_untyped", state="synced", medusa_id="V1", type_="")  # synced, texture live, NO type
        ledger.upsert("product", {"sku": "P-1", "source": "s", "variation_key": "slab_untyped",
                                  "state": "pending", "created_at": now_iso(), "updated_at": now_iso()},
                      pk=("sku",))
        assert ready(ledger, "products") == [], "product on an untyped variation must be held"
        ledger.execute("UPDATE variation SET type = 'Marble' WHERE key = 'slab_untyped'")
        assert [r["external_id"] for r in ready(ledger, "products")] == ["P-1"]


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


def test_server_self_seeds_a_missing_ledger(tmp_path, monkeypatch):
    # a fresh host (ECS on a new EFS volume) has no ledger -- the sync server must CREATE one and start,
    # not refuse. bootstrap_ledger_if_missing leaves a servable ledger; a produce populates it later.
    from stone_pipeline.ledger import server, writethrough
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "development.db"))
    monkeypatch.setenv("BLOKPORT_ENV", "development")
    path = writethrough.ledger_path()
    assert not path.exists()
    server.bootstrap_ledger_if_missing(path)
    assert path.exists()
    with Ledger.open(path, env="development") as lg:
        assert "variation" in status(lg)          # a real, servable ledger
    server.bootstrap_ledger_if_missing(path)       # idempotent: a no-op second time
    assert path.exists()


def test_gate_state_counts_held_and_untyped(tmp_path):
    # the produce catalog-gate reconciliation reads these from the ledger (schema owned here, not in
    # produce): held = new-pending variations, untyped = held ones lacking a type. No dangling count:
    # variation_key is a FK (foreign_keys=ON), so a variation-less product cannot exist.
    from stone_pipeline.ledger.sync import gate_state
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, "slab_held_1", "pending", medusa_id=None, type_="Marble")   # held, typed
        _variation(lg, "slab_untyped_2", "pending", medusa_id=None, type_="")       # held + untyped
        _variation(lg, "slab_synced_3", "synced", medusa_id="M1", type_="Marble")   # not held (has id)
        _product(lg, "SKU-2", "slab_held_1", "pending")                             # a normal product
        assert gate_state(lg) == (2, 1)


def test_requeue_endpoint_recovers_dead_lettered(tmp_path):
    # POST /sync/v1/requeue is the HTTP lever over requeue_dead_lettered: un-quarantine gap_held -> dirty
    # so the entity re-serves after the Medusa-side cause is fixed. Mirrors GET /sync/v1/failures (observe).
    from stone_pipeline.ledger import server
    from stone_pipeline.ledger.sync import ack, _MAX_SYNC_ATTEMPTS
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V1")
        _product(ledger, "P-1", "slab_v1", state="pending")
        for _ in range(_MAX_SYNC_ATTEMPTS):
            ack(ledger, "products", "P-1", status_="failed", reason="Medusa 400: bad company")
        assert ledger.get("product", "sku", "P-1")["state"] == "gap_held"
        code, body = server.dispatch(ledger, "POST", "requeue", {}, {})
        assert code == 200 and body["requeued"] == 1
        assert ledger.get("product", "sku", "P-1")["state"] == "dirty"      # re-served
        assert server.dispatch(ledger, "POST", "requeue", {}, {"type": "bogus"})[0] == 400  # bad type -> 400
