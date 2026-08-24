"""Product equivalence: populate the ledger from canonical rows, render it back,
and assert it reproduces emit's import CSV byte-for-byte (design section 13).

Unlike variants (Key-only), products are id-bearing, so this is the fixture-based
populate+render test: the same canonical rows go through emit directly (CSV-A) and
through ledger populate+render (CSV-B); the bytes must match. Hermetic (a synthetic
id seed), so no pipeline run or live data is needed.
"""

from __future__ import annotations

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_products
from stone_pipeline.ledger.render import render_products
from stone_pipeline.stages import emit


def _seed_ids(ledger: Ledger) -> None:
    now = now_iso()
    for cat, val, mid in [
        ("color", "Black", "C1"), ("finish", "Polished", "F1"), ("finish", "Honed", "F2"),
        ("quality", "First", "Q1"), ("type", "Marble", "T1"),
        ("category", "Slabs", "PC1"), ("category", "Blocks", "PC2"),
    ]:
        ledger.upsert("attribute", {
            "category": cat, "value": val, "medusa_id": mid, "state": "synced",
            "created_at": now, "updated_at": now,
        }, pk=("category", "value"))
    for key, mid, name in [
        ("slab_marble_carrara_white_0001", "V1", "Carrara White"),
        ("block_marble_nero_0002", "V2", "Nero"),
    ]:
        ledger.upsert("variation", {
            "key": key, "branch": key.split("_", 1)[0], "type": "Marble", "name": name,
            "aliases": "[]", "image_url": "", "image_sha256": None, "image_model": None,
            "volume": "", "medusa_id": mid, "payload_hash": "", "state": "synced",
            "first_seen": now, "last_synced": now, "created_at": now, "updated_at": now,
        }, pk=("key",))


def _fixture_rows() -> list[CanonicalRow]:
    slab = CanonicalRow(
        src_site="polonine", surrogate_key="AAA",
        color_name="Black", color_id="C1", finish_name="Polished", finish_id="F1",
        quality_name="First", quality_id="Q1", type_name="Marble", type_id="T1",
        variation_id="V1", variation_name="Carrara White", category_pcat_id="PC1",
        title="Carrara White Polished Slab", description="A fine marble.",
        handle="carrara-white-polished-slab-polonine-aaa",
        slug="carrara-white-polished-slab-polonine-aaa",
        weight=0.3, length=2.5, width=0.2, height=2.0,
        origin_country_code="IT", origin_city="Carrara", origin_county="Tuscany",
        company_id="CO1", sales_channel_id="SC1", port_ids=["P1", "P2"],
        bundle_size=7, sold_in_bundle=True, raw_slab_count="7",
        thumbnail_key="t.jpg", product_image_keys=["i1.jpg", "i2.jpg"], oriented_image_keys=[],
    )
    block = CanonicalRow(
        src_site="polonine", surrogate_key="BBB",
        color_name="Black", color_id="C1", finish_name="Honed", finish_id="F2",
        quality_name="First", quality_id="Q1", type_name="Marble", type_id="T1",
        variation_id="V2", variation_name="Nero", category_pcat_id="PC2",
        title="Nero Honed Block", description="A solid block.",
        handle="nero-honed-block-polonine-bbb", slug="nero-honed-block-polonine-bbb",
        weight=20.0, length=2.0, width=1.5, height=2.5,
        origin_country_code="IT",
        company_id="CO1", sales_channel_id="SC1", port_ids=["P1"],
        bundle_size=None, sold_in_bundle=False, raw_slab_count="1",
        thumbnail_key="front.jpg",
        oriented_image_keys=["front.jpg", "right.jpg", "back.jpg", "left.jpg"],
        product_image_keys=[],
    )
    return [slab, block]


def test_products_populate_render_equals_emit(tmp_path):
    cfg = load_source("polonine")
    columns = emit.read_template_columns()
    rows = _fixture_rows()

    direct = tmp_path / "A.csv"
    emit.write_import_csv(rows, cfg, direct, columns)

    via_ledger = tmp_path / "B.csv"
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_ids(ledger)
        assert populate_products(ledger, rows, cfg) == len(rows)
        render_products(ledger, cfg, via_ledger, columns)

    assert via_ledger.read_bytes() == direct.read_bytes(), (
        "product populate+render is NOT byte-identical to emit"
    )


def test_repopulate_preserves_synced_products(tmp_path):
    # regression: a write-through re-run must NOT reset a synced product to pending or wipe its
    # acked medusa_id. NEW -> pending; UNCHANGED re-run -> stays synced (untouched); a real content
    # CHANGE -> dirty (re-serve) with the id preserved. The old code stomped all three.
    cfg = load_source("polonine")
    rows = _fixture_rows()
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_ids(ledger)
        populate_products(ledger, rows, cfg)                       # NEW
        sku = emit._sku(rows[0], cfg)
        assert ledger.get("product", "sku", sku)["state"] == "pending"

        # Medusa acks an id
        ledger.execute("UPDATE product SET state='synced', medusa_id='prod_1', last_synced=? "
                       "WHERE sku=?", (now_iso(), sku))
        # UNCHANGED re-run: stays synced, id intact
        populate_products(ledger, rows, cfg)
        row = ledger.get("product", "sku", sku)
        assert row["state"] == "synced" and row["medusa_id"] == "prod_1", "re-run stomped a synced product"

        # CHANGED re-run: dirty (re-serve), id preserved
        changed = [rows[0].model_copy(update={"title": "Carrara White Polished Slab v2"})] + rows[1:]
        populate_products(ledger, changed, cfg)
        row = ledger.get("product", "sku", sku)
        assert row["state"] == "dirty" and row["medusa_id"] == "prod_1", "a change must dirty, keep id"


def test_collection_location_stored_and_dirties_on_change(tmp_path):
    # the supplier collection location is stored on the product AND folded into the payload_hash, so a
    # config change flips the product dirty and re-serves. A value NOT in the hash would never re-serve
    # (the variant-image-freshness bug); this guards against that regression.
    cfg = load_source("polonine")
    coll = [r.model_copy(update={"collection_country_code": "IT", "collection_city": "Verona"})
            for r in _fixture_rows()]
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _seed_ids(ledger)
        populate_products(ledger, coll, cfg)
        sku = emit._sku(coll[0], cfg)
        p = ledger.get("product", "sku", sku)
        assert p["collection_country_code"] == "IT" and p["collection_city"] == "Verona"

        ledger.execute("UPDATE product SET state='synced', medusa_id='prod_1', last_synced=? WHERE sku=?",
                       (now_iso(), sku))
        changed = [coll[0].model_copy(update={"collection_city": "Milan"})] + coll[1:]
        populate_products(ledger, changed, cfg)
        row = ledger.get("product", "sku", sku)
        assert row["state"] == "dirty" and row["medusa_id"] == "prod_1", "a collection change must dirty"
        assert row["collection_city"] == "Milan"
