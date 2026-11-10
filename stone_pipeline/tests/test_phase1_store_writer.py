"""SC-3's writer (plan SC-3-SC-4-PLAN.md sections 1, 1c, 1d, 4a) on fixture stores: an old ledger made with the real
v14 DDL, an old config.db, the flat icons and the best-model marker, and a versioned in-memory S3 for the one-off run.

The writer turns a variety's copies into ONE v15 row on its new Key (the survivor's copy, dirty) with its icons, moves
products to the new Key and their category to its handle, drops and lists variation tombstones, moves retired and
protected Keys, copies each flat icon to its pair add-only, and writes nothing when anything aborts. The run reads
every store by its step-1 VersionId, refuses a store that moved on with other content, writes only on MIGRATE_WRITE=1
with the dry run's digests, and never writes an old store key.
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from stone_pipeline.config import store
from stone_pipeline.io.variety_icons import VARIATIONS_PREFIX
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.phase1 import migrate_run, store_writer
from stone_pipeline.phase1.migrate_stores import MigrationRefused, read_key_map
from stone_pipeline.phase1.variety_key import new_key

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-11-10T00:00:00+00:00"
NEW = new_key("Marble", "Carrara")
OLD_SLAB, OLD_BLOCK, OLD_TILE = "slab_marble_carrara_1", "block_marble_carrara_2", "tile_marble_carrara_3"
GHOST = "slab_marble_ghost_9"
NEW_GHOST = new_key("Marble", "Ghost")


def _key_map_bytes():
    rows = [("old_key", "new_key", "category_rank", "type_name"), (OLD_SLAB, NEW, "0", "Marble"),
            (OLD_BLOCK, NEW, "1", "Marble"), (OLD_TILE, NEW, "2", "Marble"), (GHOST, NEW_GHOST, "0", "Marble")]
    return "".join(",".join(r) + "\n" for r in rows).encode()


def _key_map():
    data = _key_map_bytes()
    return read_key_map(data, hashlib.sha256(data).hexdigest())


SURVIVORS = {OLD_SLAB: "V-SLAB", OLD_BLOCK: "V-SLAB", OLD_TILE: "V-SLAB"}


def _old_ledger(path, *, extra_sql=()):
    conn = sqlite3.connect(str(path))
    conn.executescript((FIXTURES / "ledger_schema_v14.sql").read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version = 14")
    conn.execute("INSERT INTO ledger_meta VALUES (1, 'development', 'fp', 14, ?, ?)", (NOW, NOW))
    for key, branch, mid, aliases, state in [(OLD_SLAB, "slab", "V-SLAB", '["Carrara C"]', "synced"),
                                             (OLD_BLOCK, "block", "V-BLOCK", '["carrara c", "Bianco"]', "synced"),
                                             (OLD_TILE, "tile", "V-TILE", "[]", "synced")]:
        conn.execute("INSERT INTO variation (key, branch, type, name, aliases, image_url, medusa_id, in_full, state, "
                     "first_seen, created_at, updated_at) VALUES (?, ?, 'Marble', 'Carrara', ?, 'u', ?, 1, ?, ?, ?, ?)",
                     (key, branch, aliases, mid, state, NOW, NOW, NOW))
    conn.execute("INSERT INTO attribute (category, value, medusa_id, state, created_at, updated_at) "
                 "VALUES ('category', 'Blocks', 'PC2', 'synced', ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO product (sku, source, variation_key, category, bundle_size, state, created_at, "
                 "updated_at) VALUES ('SEN-1', 'sen', ?, 'Blocks', 6, 'synced', ?, ?)", (OLD_BLOCK, NOW, NOW))
    conn.execute("INSERT INTO inventory (sku, qty, updated_at) VALUES ('SEN-1', 3, ?)", (NOW,))
    conn.execute("INSERT INTO removed (external_id, kind, reason, created_at, updated_at) "
                 "VALUES ('slab_marble_old_5', 'variation', 'reseed_dedup', ?, ?)", (NOW, NOW))
    conn.execute("INSERT INTO removed (external_id, kind, source, reason, created_at, updated_at) "
                 "VALUES ('SEN-9', 'product', 'sen', 'discontinued', ?, ?)", (NOW, NOW))
    for sql in extra_sql:
        conn.execute(sql)
    conn.commit()
    conn.close()
    return path


PAIRS = {NEW: {"slab": "e-s", "block": "e-b"}}


# --- the ledger -----------------------------------------------------------------------------------------------------

def test_a_variety_s_copies_become_one_dirty_v15_row_with_its_icons(tmp_path):
    new_db = tmp_path / "new.db"
    report = store_writer.build_ledger(_old_ledger(tmp_path / "old.db"), new_db, _key_map(), SURVIVORS, set(), PAIRS)
    assert report.aborts == {}
    with Ledger.open(new_db, env="development") as lg:            # the v15 DAL opens it: the env guard holds
        rows = [dict(r) for r in lg.execute("SELECT key, type, name, aliases, medusa_id, state FROM variation")]
        icons = [tuple(r) for r in lg.execute("SELECT category_handle, s3_key, etag FROM variation_category_image")]
        product = lg.get("product", "sku", "SEN-1")
        removed = [r["external_id"] for r in lg.execute("SELECT external_id FROM removed")]
        assert lg.get("inventory", "sku", "SEN-1")["qty"] == 3
    assert rows == [{"key": NEW, "type": "Marble", "name": "Carrara", "aliases": '["Carrara C", "Bianco"]',
                     "medusa_id": "V-SLAB", "state": "dirty"}]
    assert icons == [("blocks", f"{VARIATIONS_PREFIX}{NEW}/block.png", "e-b"),
                     ("slabs", f"{VARIATIONS_PREFIX}{NEW}/slab.png", "e-s")]
    assert (product["variation_key"], product["category"], product["bundle_size"]) == (NEW, "blocks", 6)
    assert removed == ["SEN-9"]                                     # product tombstones stay
    assert report.listed["dropped_variation_tombstones"] == [
        {"old_key": "slab_marble_old_5", "new_key": None, "reason": "reseed_dedup"}]


def test_the_new_export_s_archived_status_is_the_survivor_s(tmp_path):
    new_db = tmp_path / "new.db"
    store_writer.build_ledger(_old_ledger(tmp_path / "old.db"), new_db, _key_map(), SURVIVORS, {NEW}, PAIRS)
    with Ledger.open(new_db, env="development") as lg:
        assert lg.get("variation", "key", NEW)["archived"] == 1


@pytest.mark.parametrize("sql, kind", [
    ("UPDATE variation SET state = 'retiring' WHERE key = 'tile_marble_carrara_3'", "unsafe_state"),
    ("INSERT INTO combination (combo_key, category, type, variation_key, color, finish, quality, state, created_at, "
     "updated_at) VALUES ('c', 'Slabs', 'Marble', 'slab_marble_carrara_1', 'W', 'P', 'A', 'synced', 'n', 'n')",
     "combination_not_empty"),
    ("UPDATE product SET category = 'Sinks'", "unknown_category"),
])
def test_an_abort_writes_no_ledger(tmp_path, sql, kind):
    new_db = tmp_path / "new.db"
    report = store_writer.build_ledger(_old_ledger(tmp_path / "old.db", extra_sql=[sql]), new_db, _key_map(),
                                       SURVIVORS, set(), PAIRS)
    assert kind in report.aborts and not new_db.exists()


def test_the_same_old_ledger_gives_the_same_logical_digest(tmp_path):
    old = _old_ledger(tmp_path / "old.db")
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    store_writer.build_ledger(old, a, _key_map(), SURVIVORS, set(), PAIRS)
    store_writer.build_ledger(old, b, _key_map(), SURVIVORS, set(), PAIRS)
    assert store_writer.sqlite_digest(a) == store_writer.sqlite_digest(b)


# --- the icons and the marker ---------------------------------------------------------------------------------------

def _flat(key):
    return f"{VARIATIONS_PREFIX}{key}.png"


def test_each_flat_icon_is_planned_onto_its_pair_add_only():
    listing = {_flat(OLD_SLAB): "e-s", _flat(OLD_BLOCK): "e-b", f"{VARIATIONS_PREFIX}{NEW}/block.png": "e-b",
               _flat("slab_marble_unknown_7"): "x", f"{VARIATIONS_PREFIX}_refreshed.json": "m"}
    plan = store_writer.plan_icon_copies(listing, _key_map())
    assert plan.copies == [(_flat(OLD_SLAB), f"{VARIATIONS_PREFIX}{NEW}/slab.png", "e-s")]   # block: already there
    assert plan.pairs == {NEW: {"slab": "e-s", "block": "e-b"}}
    assert plan.report.listed["icon_not_in_map"] == ["slab_marble_unknown_7"]
    assert plan.report.counts["icon_unchanged"] == 1 and plan.report.aborts == {}


def test_a_pair_already_there_with_other_bytes_aborts():
    listing = {_flat(OLD_SLAB): "e-s", f"{VARIATIONS_PREFIX}{NEW}/slab.png": "other"}
    assert "icon_etag_conflict" in store_writer.plan_icon_copies(listing, _key_map()).report.aborts


def test_two_old_icons_on_one_pair_abort():
    data = _key_map_bytes() + b"slab_marble_carrara_twin_4," + NEW.encode() + b",0,Marble\n"
    key_map = read_key_map(data, hashlib.sha256(data).hexdigest())
    listing = {_flat(OLD_SLAB): "e1", _flat("slab_marble_carrara_twin_4"): "e2"}
    assert store_writer.plan_icon_copies(listing, key_map).report.aborts["icon_pair_collision"] == sorted(listing)


def test_the_marker_s_old_keys_become_pair_ids():
    pairs, report = store_writer.remap_marker([OLD_TILE, OLD_SLAB], _key_map())
    assert pairs == [f"{NEW}/slab", f"{NEW}/tile"] and report.aborts == {}
    assert "marker_unmapped_key" in store_writer.remap_marker(["slab_marble_nope_1"], _key_map())[1].aborts


# --- config.db ------------------------------------------------------------------------------------------------------

def _old_config(path, retired=(), protected=()):
    conn = store._connect(path)                        # a real config.db (v12 tables), stamped back to 11
    for key in retired:
        conn.execute("INSERT INTO retired_variation (key, created_at) VALUES (?, ?)", (key, NOW))
    for key in protected:
        conn.execute("INSERT INTO protected_variation (key, decided_at) VALUES (?, ?)", (key, NOW))
    conn.execute("INSERT INTO review_pending (kind, ref, payload, updated_at) VALUES ('mint', 'r', '{}', ?)", (NOW,))
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    conn.execute("PRAGMA journal_mode = DELETE")       # one self-contained file, as the snapshot uploads it
    conn.close()
    return path


def test_a_whole_retirement_moves_to_the_new_key_and_the_store_is_stamped_12(tmp_path):
    old = _old_config(tmp_path / "old.db", retired=[OLD_SLAB, OLD_BLOCK, OLD_TILE], protected=[GHOST])
    report = store_writer.migrate_config(old, tmp_path / "new.db", _key_map(), {OLD_SLAB, OLD_BLOCK, OLD_TILE})
    assert report.aborts == {}
    conn = store._connect(tmp_path / "new.db")                     # the v12 store opens it
    assert [r[0] for r in conn.execute("SELECT key FROM retired_variation")] == [NEW]
    assert [r[0] for r in conn.execute("SELECT key FROM protected_variation")] == [NEW_GHOST]
    assert conn.execute("SELECT COUNT(*) FROM review_pending").fetchone()[0] == 0
    conn.close()


def test_a_partial_retirement_aborts_and_writes_nothing(tmp_path):
    old = _old_config(tmp_path / "old.db", retired=[OLD_SLAB])
    report = store_writer.migrate_config(old, tmp_path / "new.db", _key_map(), {OLD_SLAB, OLD_BLOCK, OLD_TILE})
    assert "partial_retirement" in report.aborts and not (tmp_path / "new.db").exists()


# --- the one-off run ------------------------------------------------------------------------------------------------

class _FakeS3:
    """A versioned bucket: key -> [(version_id, bytes)], latest last."""

    def __init__(self):
        self.objects: dict[str, list[tuple[str, bytes]]] = {}
        self.n = 0

    def add(self, key, data):
        self.n += 1
        self.objects.setdefault(key, []).append((f"v{self.n}", data))
        return f"v{self.n}"

    def _err(self, code):
        exc = Exception(code)
        exc.response = {"Error": {"Code": code}}
        return exc

    def get_object(self, Bucket, Key, VersionId):
        return {"Body": io.BytesIO(dict(self.objects[Key])[VersionId])}

    def head_object(self, Bucket, Key):
        if not self.objects.get(Key):
            raise self._err("404")
        return {"VersionId": self.objects[Key][-1][0]}

    def get_paginator(self, name):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k, "ETag": f'"{hashlib.md5(v[-1][1]).hexdigest()}"'}
                                      for k, v in sorted(objects.items()) if k.startswith(Prefix) and v]}]
        return _P()

    def put_object(self, Bucket, Key, Body):
        return {"VersionId": self.add(Key, Body.read())}

    def copy_object(self, Bucket, Key, CopySource):
        data = self.objects[CopySource["Key"]][-1][1]
        self.add(Key, data)
        return {"CopyObjectResult": {"ETag": f'"{hashlib.md5(data).hexdigest()}"'}}

    def delete_object(self, Bucket, Key):
        self.objects[Key].append(("deleted", b""))
        self.objects[Key] = []


def _spec(s3, key, data):
    return {"key": key, "version_id": s3.add(key, data), "sha256": hashlib.sha256(data).hexdigest()}


def _stack(tmp_path):
    """A dev stack at step 1: its stores, the maps, the new export, the flat icons and the marker."""
    s3 = _FakeS3()
    km = _key_map_bytes()
    sv = json.dumps({"keyMapSha256": hashlib.sha256(km).hexdigest(),
                     "survivors": [{"oldKey": k, "survivorId": v} for k, v in SURVIVORS.items()]}).encode()
    export = f"Id,Key,Name,Aliases,status,Type,owner\nV-SLAB,{NEW},Carrara,,active,Marble,scraper\n".encode()
    inputs = {"key_map": _spec(s3, "release-input/km.csv", km), "survivor_map": _spec(s3, "release-input/sv.json", sv),
              "export": _spec(s3, "dev/scraper/from_medusa/variants_export.csv", export), "stores": {}}
    base = f"Key,Name,Type,Aliases\n{NEW},Carrara,Marble,\n".encode()
    files = {"ledger": _old_ledger(tmp_path / "old.db").read_bytes(),
             "config": _old_config(tmp_path / "oldc.db").read_bytes(),
             "writeback": b"variation_id,alias,method\nV-BLOCK,Carara,fuzzy\n",
             "refreshed": json.dumps([OLD_SLAB]).encode(), "base_from_medusa": base, "base_to_upload": base,
             "outputs": b"tar"}
    for name, data in files.items():
        inputs["stores"][name] = {"version_id": s3.add(migrate_run.STORES[name][0], data)}
    s3.add(_flat(OLD_SLAB), b"slab-png")
    return s3, inputs


def test_the_dry_run_decides_everything_and_writes_nothing(tmp_path):
    s3, inputs = _stack(tmp_path)
    before = {k: list(v) for k, v in s3.objects.items()}
    result = migrate_run.run(s3, inputs, write=False)
    assert result["aborts"] == {} and result["counts"]["variation"] == 1 and result["counts"]["icon_copies"] == 1
    assert s3.objects == before


def test_the_real_run_writes_the_v2_stores_and_never_an_old_store_key(tmp_path):
    s3, inputs = _stack(tmp_path)
    dry = migrate_run.run(s3, inputs, write=False)
    for name, digest in dry["step1_digests"].items():
        inputs["stores"][name]["digest"] = digest
    old_versions = {name: list(s3.objects[key]) for name, (key, _) in migrate_run.STORES.items()
                    if name in ("ledger", "config", "writeback")}
    result = migrate_run.run(s3, inputs, write=True)
    assert result["written"]["icons_copied"] == 1
    assert s3.objects[f"{VARIATIONS_PREFIX}{NEW}/slab.png"][-1][1] == b"slab-png"
    assert s3.objects[_flat(OLD_SLAB)][-1][1] == b"slab-png"             # the flat icon stays until SC-5
    for name, key in migrate_run.NEW_STORES.items():
        assert s3.objects[key], f"{name}.v2 written"
        assert s3.objects[migrate_run.STORES[name][0]] == old_versions[name]   # the old key is untouched
    assert s3.objects[migrate_run.STORES["outputs"][0]] == []               # wiped
    marker = migrate_run.STORES["refreshed"][0]
    assert json.loads(s3.objects[marker][-1][1]) == [f"{NEW}/slab"]
    # L2: a re-run after the in-place objects are restored by version (the marker; outputs stays wiped) finds the
    # `.v2` stores unchanged and the icon pair already there
    s3.add(marker, json.dumps([OLD_SLAB]).encode())
    again = migrate_run.run(s3, {**inputs, "stores": {**inputs["stores"], "outputs": {"version_id": None}}},
                            write=True)
    assert all(again["written"][n]["unchanged"] for n in migrate_run.NEW_STORES)
    assert again["written"]["icons_copied"] == 0


def test_the_real_run_refuses_without_the_dry_run_s_digests(tmp_path):
    s3, inputs = _stack(tmp_path)
    with pytest.raises(MigrationRefused, match="dry run"):
        migrate_run.run(s3, inputs, write=True)


def test_a_store_written_since_step_1_is_refused(tmp_path):
    s3, inputs = _stack(tmp_path)
    s3.add(migrate_run.STORES["writeback"][0], b"variation_id,alias,method\nV-TILE,Carrera,fuzzy\n")
    with pytest.raises(MigrationRefused, match="moved on"):
        migrate_run.run(s3, inputs, write=False)


def test_a_same_content_re_upload_since_step_1_passes(tmp_path):
    s3, inputs = _stack(tmp_path)
    key = migrate_run.STORES["writeback"][0]
    s3.add(key, s3.objects[key][-1][1])
    assert migrate_run.run(s3, inputs, write=False)["aborts"] == {}
