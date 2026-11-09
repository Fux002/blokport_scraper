"""VE2 PR3: the archived-variation freeze (design section 6).

variation.archived is a PURE FUNCTION of the Medusa variants export, mirrored at FETCH time (before any source
is recorded) and again at catalog (for the rows it creates). The variations lane serves archived = 0 only; the
products lane serves an archived variety's product only when Medusa already holds it, so a NEW product on a
frozen variety is never served. An ack RECORDS what Medusa did, so it accepts a synced OR archived variation
(it never re-applies the serve-time freeze). gate_state excludes archived, so a reset still converges.
Inventory is unchanged. attribute.restricted is NOT kept in the ledger here (PR4 owns restricted values).
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

def test_fresh_ledger_has_archived_defaulting_to_zero(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        vcols = {r["name"]: r for r in ledger.execute("PRAGMA table_info(variation)")}
        assert "archived" in vcols
        assert vcols["archived"]["dflt_value"] == "0" and vcols["archived"]["notnull"] == 1
        # restricted is deliberately NOT a ledger column (PR4 decides restricted values from the files)
        assert "restricted" not in {r["name"] for r in ledger.execute("PRAGMA table_info(attribute)")}
        assert ledger.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_v13_ledger_gains_archived_and_keeps_its_data(tmp_path):
    if sqlite3.sqlite_version_info < (3, 35, 0):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite >= 3.35 to simulate the v13 shape")
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _variation(lg, "slab_v1", archived=0)
    raw = sqlite3.connect(p)                                       # simulate a v13 ledger
    raw.execute("ALTER TABLE variation DROP COLUMN archived")
    raw.execute("PRAGMA user_version = 13")
    raw.execute("UPDATE ledger_meta SET schema_version = 13")
    raw.commit()
    raw.close()
    with Ledger.open(p, env="development") as lg:
        assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert lg.get("variation", "key", "slab_v1")["archived"] == 0
    with Ledger.open(p, env="development") as lg:   # idempotent: a second open is a no-op
        assert lg.get("variation", "key", "slab_v1")["archived"] == 0


# --- B. the mirror (pure function of the export) -----------------------------

def test_mirror_sets_and_clears_variation_archived(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", archived=0)
        _variation(ledger, "slab_v2", archived=0)
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "archived"), ("i2", "slab_v2", "B", "active")])
        assert writethrough.mirror_archived_variations(ledger, export_path=exp) == 1
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 1
        assert ledger.get("variation", "key", "slab_v2")["archived"] == 0
        # flips back to 0 when the export no longer archives it (pure function, not sticky)
        exp2 = _export_csv(tmp_path, [("i1", "slab_v1", "A", "active"), ("i2", "slab_v2", "B", "active")])
        assert writethrough.mirror_archived_variations(ledger, export_path=exp2) == 1
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 0


def test_mirror_touches_only_changed_rows(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", archived=1)
        before = ledger.get("variation", "key", "slab_v1")["updated_at"]
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "archived")])   # unchanged: stays archived
        assert writethrough.mirror_archived_variations(ledger, export_path=exp) == 0
        after = ledger.get("variation", "key", "slab_v1")
        assert after["archived"] == 1 and after["updated_at"] == before   # no spurious write


def test_an_unarchived_synced_row_is_served_exactly_once(tmp_path):
    # P2-10 / 173 F18e: an archived variety was never served, so the consumer may hold it without its current payload.
    # Lowering archived (1 to 0) marks the row dirty in the same UPDATE, so it re-serves once with its whole payload.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", archived=1)
        _variation(ledger, "slab_v2", state="synced", archived=0)            # never archived: untouched
        assert sync.ready(ledger, "variations") == []
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "active"), ("i2", "slab_v2", "B", "active")])
        assert writethrough.mirror_archived_variations(ledger, export_path=exp) == 1
        assert ledger.get("variation", "key", "slab_v1")["state"] == "dirty"
        assert ledger.get("variation", "key", "slab_v2")["state"] == "synced"
        served = sync.ready(ledger, "variations")
        assert [i["external_id"] for i in served] == ["slab_v1"]
        sync.ack(ledger, "variations", "slab_v1", "V", payload_hash=served[0]["payload_hash"])
        assert sync.ready(ledger, "variations") == []                         # once, not on every pull
        before = [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")]
        assert writethrough.mirror_archived_variations(ledger, export_path=exp) == 0   # a second run: no change
        assert [dict(r) for r in ledger.execute("SELECT * FROM variation ORDER BY key")] == before
        assert sync.ready(ledger, "variations") == []


def test_unarchive_outranks_a_lease_issued_before_it(tmp_path):
    # the #591 review's race on the mirror: the produce runs it in another process than the pulls, so a lease can be
    # in flight across an archive and un-archive. That lease's ack, echoing the same hash, records what Medusa did
    # but must not consume the un-archive's re-serve: the row stays dirty and the next pull serves it once.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="dirty", archived=0)
        leased = sync.ready(ledger, "variations")                             # the lease, before the flip
        assert [i["external_id"] for i in leased] == ["slab_v1"]
        for status in ("archived", "active"):                                 # archive, then un-archive
            writethrough.mirror_archived_variations(
                ledger, export_path=_export_csv(tmp_path, [("i1", "slab_v1", "A", status)]))
        assert sync.ack(ledger, "variations", "slab_v1", "V-2", payload_hash=leased[0]["payload_hash"]) == 1
        row = ledger.get("variation", "key", "slab_v1")
        assert row["state"] == "dirty" and row["medusa_id"] == "V-2"           # recorded, not synced
        served = sync.ready(ledger, "variations")
        assert [i["external_id"] for i in served] == ["slab_v1"]
        sync.ack(ledger, "variations", "slab_v1", "V-2", payload_hash=served[0]["payload_hash"])
        assert ledger.get("variation", "key", "slab_v1")["state"] == "synced"
        assert sync.ready(ledger, "variations") == []                         # once


def test_unarchive_never_revives_an_operator_abandoned_row(tmp_path):
    # the same re-mark rule as a reserve: an abandoned dead-letter is the operator's terminal verdict, kept as it is
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="gap_held", archived=1)
        ledger.execute("UPDATE variation SET abandoned_at = '2000-01-01T00:00:00+00:00'")
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "active")])
        assert writethrough.mirror_archived_variations(ledger, export_path=exp) == 1
        row = ledger.get("variation", "key", "slab_v1")
        assert row["archived"] == 0 and row["state"] == "gap_held" and row["abandoned_at"]
        assert sync.ready(ledger, "variations") == []


def test_unarchive_never_resurrects_a_retiring_row_or_demotes_a_pending_one(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="retiring", archived=1)
        _variation(ledger, "slab_v2", state="pending", medusa_id=None, archived=1)
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "active"), ("i2", "slab_v2", "B", "active")])
        assert writethrough.mirror_archived_variations(ledger, export_path=exp) == 2
        assert ledger.get("variation", "key", "slab_v1")["state"] == "retiring"
        assert ledger.get("variation", "key", "slab_v2")["state"] == "pending"


def test_mirror_is_a_noop_when_the_export_is_absent(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", archived=0)
        assert writethrough.mirror_archived_variations(ledger, export_path=tmp_path / "absent.csv") == 0
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 0


def test_mirror_freezes_a_variety_archived_by_any_export_path(tmp_path):
    # the consumer may archive via a staff merge; the mirror keys purely off the export status, not the cause
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="dirty", archived=0)
        exp = _export_csv(tmp_path, [("i1", "slab_v1", "A", "archived")])
        writethrough.mirror_archived_variations(ledger, export_path=exp)
        assert ledger.get("variation", "key", "slab_v1")["archived"] == 1
        assert sync.count_ready(ledger, "variations") == 0


def test_produce_mirror_hook_runs_the_mirror_at_fetch(monkeypatch, tmp_path):
    # the fetch-time hook (produce._mirror_archived) must open the ledger and run the mirror, so the freeze is
    # applied BEFORE any record_source write (the HIGH finding). A failing-first guard for that call site.
    import contextlib

    import stone_pipeline.produce as produce
    seen = []

    @contextlib.contextmanager
    def _fake_open(path=None):
        with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
            yield lg

    monkeypatch.setattr(writethrough, "enabled", lambda: True)
    monkeypatch.setattr(writethrough, "open_ledger", _fake_open)
    monkeypatch.setattr(writethrough, "mirror_archived_variations", lambda ledger, **kw: seen.append(ledger) or 0)
    produce._mirror_archived()
    assert len(seen) == 1
    # and it is a no-op when write-through is off (no ledger opened, no mirror)
    seen.clear()
    monkeypatch.setattr(writethrough, "enabled", lambda: False)
    produce._mirror_archived()
    assert seen == []


def test_record_catalog_mirrors_the_rows_it_creates(tmp_path, monkeypatch):
    # finding 3: the SECOND mirror call, in record_catalog AFTER populate, must freeze a variety whose ledger
    # row first appears THERE (populate_variations_full created it). Deleting that call leaves it served.
    import types

    from stone_pipeline.ledger import populate
    ledger_file = tmp_path / "dev.ledger"
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(ledger_file))
    # pre-create the ledger (non-empty) so open_ledger does NOT bootstrap from the real exports
    with Ledger.open(ledger_file, env="development") as lg:
        _variation(lg, "slab_seed", archived=0)
    full = tmp_path / "1_variants_full.csv"
    full.write_text("Key\nslab_K\n", encoding="utf-8")   # record_catalog gates on this file existing
    export = _export_csv(tmp_path, [("i", "slab_K", "K", "archived")])
    monkeypatch.setattr(writethrough, "SETTINGS", types.SimpleNamespace(
        paths=types.SimpleNamespace(to_upload_dir=tmp_path, export_file=export)))

    def _pop(ledger, path):   # what populate_variations_full does at catalog: create the produced variety
        _variation(ledger, "slab_K", state="pending", medusa_id=None, archived=0)
        return 1
    monkeypatch.setattr(populate, "populate_variations_full", _pop)
    monkeypatch.setattr(populate, "fill_variation_types", lambda ledger: 0)

    assert writethrough.record_catalog() is True
    with Ledger.open(ledger_file, env="development") as lg:
        assert lg.get("variation", "key", "slab_K")["archived"] == 1   # the second mirror froze the new row
        assert sync.count_ready(lg, "variations") == 0                 # frozen: not served


# --- C. variations lane freeze + the produce gate ----------------------------

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


def test_gate_state_ignores_archived_held(tmp_path):
    # after a reset an archived variety is pending with no medusa_id and never re-serves; counting it held
    # would mask the reset's converged (held == 0) check, so gate_state must exclude it.
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_arch", state="pending", medusa_id=None, archived=1)
        _variation(ledger, "slab_new", state="pending", medusa_id=None, archived=0)
        held, _untyped = sync.gate_state(ledger)
        assert held == 1   # only the active new variety, never the frozen one


# --- D. products lane + ack guard + the frozen-waiting count ------------------

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


def test_product_ack_records_what_medusa_did_even_after_archival(tmp_path):
    # the variety is archived BETWEEN serve and ack; the ack must still record the id Medusa assigned, or the
    # ledger loses it and withdraw_unsent can orphan the product in Medusa (the LOW ack-guard finding).
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=0)
        _product(ledger, "P-1", "slab_v1", state="pending")
        assert [r["external_id"] for r in sync.ready_products(ledger)] == ["P-1"]   # leased to 'syncing'
        ledger.execute("UPDATE variation SET archived = 1 WHERE key = 'slab_v1'")
        assert sync.ack(ledger, "products", "P-1", medusa_id="MP") == 1
        row = ledger.get("product", "sku", "P-1")
        assert row["state"] == "synced" and row["medusa_id"] == "MP"


def test_frozen_waiting_products_counts_new_on_archived(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=1)
        _product(ledger, "P-new", "slab_v1", state="pending")                  # waiting
        _product(ledger, "P-old", "slab_v1", state="pending", medusa_id="MP")  # existing, not waiting
        assert sync.frozen_waiting_products(ledger) == 1


# --- E. inventory unchanged ---------------------------------------------------

def test_inventory_lane_ignores_archived(tmp_path):
    assert "archived" not in sync._INVENTORY_LANE
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=1)
        _product(ledger, "P-1", "slab_v1", state="synced", medusa_id="MP")
        now = now_iso()
        ledger.upsert("inventory", {"sku": "P-1", "qty": 5, "made_to_order": 0, "updated_at": now}, pk=("sku",))
        assert [r["external_id"] for r in sync.ready_inventory(ledger)] == ["P-1"]


# --- F. B1: status required decided only on the Medusa download path ----------

def test_status_required_is_decided_by_the_download_path(monkeypatch, tmp_path):
    other = tmp_path / "render_dump.csv"
    monkeypatch.setattr("stone_pipeline.reference.loaders.variants.VARIANTS_EXPORT_STATUS_REQUIRED", False)
    monkeypatch.setattr("stone_pipeline.reference.loaders.attributes.ATTRIBUTES_STATUS_REQUIRED", False)
    assert loaders.variants_status_required(SETTINGS.paths.export_file) is False
    assert loaders.attributes_status_required(SETTINGS.paths.attributes_csv) is False
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
        ledger.upsert("attribute", {"category": "category", "value": "Slabs", "medusa_id": "A",
                                    "state": "synced", "created_at": now_iso(), "updated_at": now_iso()},
                      pk=("category", "value"))
        _variation(ledger, "slab_v1", state="synced", medusa_id="V", archived=0)
        out = tmp_path / "2_valid_combinations.csv"
        written = render_combinations(ledger, out, backbone_paths=[], products_csv=None)
        assert isinstance(written, int) and out.exists()   # did not raise on the status-less temp dumps
