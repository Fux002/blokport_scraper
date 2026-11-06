"""VE2 PR3: the archived-variation freeze (design section 6).

variation.archived and attribute.restricted are a PURE FUNCTION of the Medusa export files, mirrored in the
write-through every produce. The variations lane serves archived = 0 only; the products lane (and the product
ack guard) accept a synced-active OR archived variation but only for an EXISTING product (one Medusa holds); a
new product on an archived variety is never served. Inventory is unchanged. Restricted attributes are mirrored
here but NOT enforced on scraped products (that is PR4, validate value_frozen).
"""

from __future__ import annotations

import sqlite3

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger import sync, writethrough
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger, now_iso
from stone_pipeline.reference import loaders

_TEX = "https://s3/tex.png"


def _variation(ledger, key, *, state="synced", medusa_id="V", archived=0, in_full=1,
               type_="Marble", image=_TEX):
    now = now_iso()
    ledger.upsert("variation", {
        "key": key, "branch": key.split("_", 1)[0], "type": type_, "name": "x", "aliases": "[]",
        "image_url": image, "image_sha256": None, "image_model": None, "volume": "",
        "medusa_id": medusa_id, "in_full": in_full, "payload_hash": "", "state": state,
        "archived": archived, "first_seen": now, "last_synced": now if medusa_id else None,
        "created_at": now, "updated_at": now}, pk=("key",))


def _product(ledger, sku, variation_key, *, state="pending", medusa_id=None, last_synced=None):
    """medusa_id or last_synced set == a product Medusa already holds (in_medusa_sql); neither == new."""
    now = now_iso()
    ledger.upsert("product", {
        "sku": sku, "source": "s", "variation_key": variation_key, "state": state,
        "medusa_id": medusa_id, "last_synced": last_synced, "created_at": now, "updated_at": now}, pk=("sku",))


def _attr(ledger, category, value, *, restricted=0, medusa_id="A"):
    now = now_iso()
    ledger.upsert("attribute", {
        "category": category, "value": value, "medusa_id": medusa_id, "state": "synced",
        "restricted": restricted, "created_at": now, "updated_at": now}, pk=("category", "value"))


def _attrs_csv(tmp_path, rows, *, with_status=True):
    """rows: (category, value, sourceid, status, internal). Writes attributes.csv and returns its Path."""
    p = tmp_path / "attributes.csv"
    header = "category,value,sourceid" + (",status,internal" if with_status else "")
    lines = [header]
    for category, value, sourceid, status, internal in rows:
        cells = [category, value, sourceid] + ([status, internal] if with_status else [])
        lines.append(",".join(cells))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _export_csv(tmp_path, rows, *, with_status=True):
    """rows: (Id, Key, Name, status). Writes variants_export.csv and returns its Path."""
    p = tmp_path / "variants_export.csv"
    header = "Id,Key,Name,Image,Aliases,Volume per kg (m³/kg)" + (",status" if with_status else "")
    lines = [header]
    for _id, key, name, status in rows:
        cells = [_id, key, name, "", "", ""] + ([status] if with_status else [])
        lines.append(",".join(cells))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# --- A. schema + migration (v14) ---------------------------------------------

def test_fresh_ledger_has_archived_and_restricted_defaulting_to_zero(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        vcols = {r["name"]: r for r in ledger.execute("PRAGMA table_info(variation)")}
        acols = {r["name"]: r for r in ledger.execute("PRAGMA table_info(attribute)")}
        assert "archived" in vcols and "restricted" in acols
        assert vcols["archived"]["dflt_value"] == "0" and vcols["archived"]["notnull"] == 1
        assert acols["restricted"]["dflt_value"] == "0" and acols["restricted"]["notnull"] == 1
        assert ledger.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_v13_ledger_gains_the_columns_and_keeps_its_data(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v13 shape")
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _variation(lg, "slab_v1", archived=0)
        _attr(lg, "color", "Beige")
    raw = sqlite3.connect(p)                                       # simulate a v13 ledger
    raw.execute("ALTER TABLE variation DROP COLUMN archived")
    raw.execute("ALTER TABLE attribute DROP COLUMN restricted")
    raw.execute("PRAGMA user_version = 13")
    raw.execute("UPDATE ledger_meta SET schema_version = 13")
    raw.commit()
    raw.close()
    with Ledger.open(p, env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert lg.get("variation", "key", "slab_v1")["archived"] == 0
        assert lg.execute("SELECT restricted FROM attribute WHERE category='color' AND value='Beige'"
                          ).fetchone()["restricted"] == 0
        # idempotent: a second open is a no-op (no duplicate-column error)
    with Ledger.open(p, env="development") as lg:
        assert lg.get("variation", "key", "slab_v1")["archived"] == 0


# --- B. the mirror (pure function of the files) ------------------------------

def test_mirror_sets_and_clears_variation_archived(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", archived=0)
        _variation(ledger, "slab_v2", archived=0)
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "archived"), ("i2", "slab_v2", "B", "active")])
        writethrough.mirror_restrictions(ledger, export_path=exp, attributes_path=None)
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 1
        assert ledger.get("variation", "key", "slab_v2")["archived"] == 0
        # flips back to 0 when the export no longer archives it (pure function, not sticky)
        exp2 = _export_csv(tmp_path, [("i1", "slab_v1", "A", "active"), ("i2", "slab_v2", "B", "active")])
        writethrough.mirror_restrictions(ledger, export_path=exp2, attributes_path=None)
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 0


def test_mirror_sets_restricted_for_archived_or_internal_attributes(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _attr(ledger, "color", "Beige")
        _attr(ledger, "finish", "Polished")
        _attr(ledger, "color", "SecretBlue")
        attrs = _attrs_csv(tmp_path, [
            ("color", "Beige", "a1", "active", "false"),
            ("finish", "Polished", "a2", "archived", "false"),
            ("color", "SecretBlue", "a3", "active", "true")])
        writethrough.mirror_restrictions(ledger, export_path=None, attributes_path=attrs)
        restricted = {(r["category"], r["value"]) for r in ledger.execute(
            "SELECT category, value FROM attribute WHERE restricted = 1")}
        assert restricted == {("finish", "Polished"), ("color", "SecretBlue")}
        # flips back when all active and not internal
        attrs2 = _attrs_csv(tmp_path, [
            ("color", "Beige", "a1", "active", "false"),
            ("finish", "Polished", "a2", "active", "false"),
            ("color", "SecretBlue", "a3", "active", "false")])
        writethrough.mirror_restrictions(ledger, export_path=None, attributes_path=attrs2)
        assert ledger.execute("SELECT COUNT(*) n FROM attribute WHERE restricted = 1").fetchone()["n"] == 0


def test_mirror_touches_only_changed_rows(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", archived=1)
        before = ledger.get("variation", "key", "slab_v1")["updated_at"]
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "archived")])   # unchanged: stays archived
        writethrough.mirror_restrictions(ledger, export_path=exp, attributes_path=None)
        after = ledger.get("variation", "key", "slab_v1")
        assert after["archived"] == 1 and after["updated_at"] == before   # no spurious write


def test_mirror_is_a_noop_when_files_are_absent(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", archived=0)
        assert writethrough.mirror_restrictions(
            ledger, export_path=tmp_path / "absent.csv", attributes_path=tmp_path / "none.csv") == (0, 0)
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 0


def test_mirror_freezes_a_variety_archived_by_any_export_path(tmp_path):
    # the consumer may archive via a staff merge; the mirror keys purely off the export status, not the cause
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="dirty", archived=0)
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "archived")])
        writethrough.mirror_restrictions(ledger, export_path=exp, attributes_path=None)
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 1
        assert sync.count_ready(ledger, "variations") == 0


# --- C. variations lane freeze ------------------------------------------------

def test_variations_lane_serves_archived_zero_only(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="pending", medusa_id=None, archived=1)
        assert sync.count_ready(ledger, "variations") == 0 and sync.ready_variations(ledger) == []
        _variation(ledger, "slab_v1", state="pending", medusa_id=None, archived=0)
        assert sync.count_ready(ledger, "variations") == 1
        assert [r["external_id"] for r in sync.ready_variations(ledger)] == ["slab_v1"]


def test_archived_beats_dirty_in_the_variations_lane(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="dirty", medusa_id="V", archived=1)
        assert sync.count_ready(ledger, "variations") == 0


# --- D. products lane + ack guard --------------------------------------------

def test_existing_product_on_archived_variation_still_serves(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=1)
        _product(ledger, "P-1", "slab_v1", state="pending", medusa_id="MP")   # Medusa holds it
        assert sync.count_ready(ledger, "products") == 1
        assert [r["external_id"] for r in sync.ready_products(ledger)] == ["P-1"]


def test_new_product_on_archived_variation_is_not_served(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=1)
        _product(ledger, "P-new", "slab_v1", state="pending")   # no medusa_id, no last_synced: new
        assert sync.count_ready(ledger, "products") == 0 and sync.ready_products(ledger) == []


def test_new_product_on_active_variation_serves(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=0)
        _product(ledger, "P-new", "slab_v1", state="pending")
        assert [r["external_id"] for r in sync.ready_products(ledger)] == ["P-new"]


def test_product_ack_guard_matches_the_lane(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=1)
        _product(ledger, "P-old", "slab_v1", state="syncing", medusa_id="MP")   # existing
        _product(ledger, "P-new", "slab_v1", state="syncing")                   # new
        assert sync.ack(ledger, "products", "P-old", medusa_id="MP") == 1
        assert ledger.get("product", "sku", "P-old")["state"] == "synced"
        assert sync.ack(ledger, "products", "P-new", medusa_id="MX") == 0        # refused by the guard
        assert ledger.get("product", "sku", "P-new")["state"] == "syncing"


# --- E. inventory unchanged ---------------------------------------------------

def test_inventory_lane_ignores_archived(tmp_path):
    assert "archived" not in sync._INVENTORY_LANE and "restricted" not in sync._INVENTORY_LANE
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=1)
        _product(ledger, "P-1", "slab_v1", state="synced", medusa_id="MP")
        now = now_iso()
        ledger.upsert("inventory", {"sku": "P-1", "qty": 5, "made_to_order": 0, "updated_at": now}, pk=("sku",))
        assert [r["external_id"] for r in sync.ready_inventory(ledger)] == ["P-1"]


# --- F. B1: status required decided only on the Medusa download path ----------

def test_status_required_is_decided_by_the_download_path(monkeypatch, tmp_path):
    other = tmp_path / "render_dump.csv"
    # flags OFF: never required, whatever the path
    monkeypatch.setattr("stone_pipeline.reference.loaders.variants.VARIANTS_EXPORT_STATUS_REQUIRED", False)
    monkeypatch.setattr("stone_pipeline.reference.loaders.attributes.ATTRIBUTES_STATUS_REQUIRED", False)
    assert loaders.variants_status_required(SETTINGS.paths.export_file) is False
    assert loaders.attributes_status_required(SETTINGS.paths.attributes_csv) is False
    # flags ON: required ONLY for the real download file, never for an internal temp dump
    monkeypatch.setattr("stone_pipeline.reference.loaders.variants.VARIANTS_EXPORT_STATUS_REQUIRED", True)
    monkeypatch.setattr("stone_pipeline.reference.loaders.attributes.ATTRIBUTES_STATUS_REQUIRED", True)
    assert loaders.variants_status_required(SETTINGS.paths.export_file) is True
    assert loaders.attributes_status_required(SETTINGS.paths.attributes_csv) is True
    assert loaders.variants_status_required(other) is False
    assert loaders.attributes_status_required(other) is False


def test_render_combinations_reads_its_temp_dumps_even_when_status_required(monkeypatch, tmp_path):
    from stone_pipeline.ledger.render import render_combinations
    # force step-C strictness; the render dumps are the OLD shape (no status column), so a non-path-aware
    # read would fail loud here. The path-aware decision keeps the internal dumps tolerant.
    monkeypatch.setattr("stone_pipeline.reference.loaders.variants.VARIANTS_EXPORT_STATUS_REQUIRED", True)
    monkeypatch.setattr("stone_pipeline.reference.loaders.attributes.ATTRIBUTES_STATUS_REQUIRED", True)
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _attr(ledger, "category", "Slabs")
        _attr(ledger, "type", "Marble")
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=0)
        out = tmp_path / "2_valid_combinations.csv"
        written = render_combinations(ledger, out, backbone_paths=[], products_csv=None)
        assert isinstance(written, int) and out.exists()   # did not raise on the status-less temp dumps
