"""SC-3's pure core (phase1.migrate_stores): what the store migration does to each Key-bearing row, decided from the
key map (SC-1) and the env's survivor map (P1-23), before any store is written. Fixture data only; the real run is
a one-off dev task (plan SC-3-SC-4-PLAN.md section 4)."""
from __future__ import annotations

import hashlib
import json

import pytest

from stone_pipeline.phase1 import migrate_stores as ms

# One identity, three copies (slab synced, block synced, tile pending); one ghost (no Medusa record anywhere).
SLAB, BLOCK, TILE = ("slab_marble_carrara_u1", "block_marble_carrara_u2", "tile_marble_carrara_u3")
NEW = "marble__carrara_n1"
G_SLAB, G_BLOCK = "slab_onyx_ghost_u4", "block_onyx_ghost_u5"
G_NEW = "onyx__ghost_n2"
KEY_MAP_CSV = ("old_key,new_key,category_rank,type_name\n"
               f"{BLOCK},{NEW},1,Marble\n{SLAB},{NEW},0,Marble\n{TILE},{NEW},2,Marble\n"
               f"{G_BLOCK},{G_NEW},1,Onyx\n{G_SLAB},{G_NEW},0,Onyx\n").encode()
KEY_MAP_SHA = hashlib.sha256(KEY_MAP_CSV).hexdigest()


def _survivor_bytes(survivors, key_map_sha=KEY_MAP_SHA):
    return json.dumps({"keyMapSha256": key_map_sha, "releaseInputSha256": "r" * 64,
                       "survivors": [{"oldKey": k, "survivorId": v} for k, v in survivors.items()]}).encode()


SURVIVORS = {SLAB: "var_B", BLOCK: "var_B", TILE: "var_B"}           # the backend kept the BLOCK copy's record (D2)


def _key_map():
    return ms.read_key_map(KEY_MAP_CSV, KEY_MAP_SHA)


def _survivors():
    data = _survivor_bytes(SURVIVORS)
    return ms.read_survivor_map(data, hashlib.sha256(data).hexdigest(), KEY_MAP_SHA)


def _variation(key, medusa_id=None, state="synced", **extra):
    return {"key": key, "medusa_id": medusa_id, "state": state, "abandoned_at": None, "archived": 0, "aliases": None,
            "in_full": 1, **extra}


# --- the two maps -----------------------------------------------------------------------------------------------------
def test_the_key_map_is_read_only_at_its_digest():
    km = _key_map()
    assert km.new_key(SLAB) == NEW and km.rank(TILE) == 2 and km.type_name(BLOCK) == "Marble"
    with pytest.raises(ms.MigrationRefused, match="sha256"):
        ms.read_key_map(KEY_MAP_CSV, "0" * 64)


def test_the_survivor_map_must_name_this_key_map_and_its_own_digest():
    data = _survivor_bytes(SURVIVORS)
    with pytest.raises(ms.MigrationRefused, match="sha256"):
        ms.read_survivor_map(data, "0" * 64, KEY_MAP_SHA)
    other = _survivor_bytes(SURVIVORS, key_map_sha="f" * 64)
    with pytest.raises(ms.MigrationRefused, match="keyMapSha256"):
        ms.read_survivor_map(other, hashlib.sha256(other).hexdigest(), KEY_MAP_SHA)
    assert _survivors() == SURVIVORS


# --- ledger variation rows (C4, D2) ---------------------------------------------------------------------------------
def test_an_identity_s_copies_collapse_to_its_survivor_copy_under_the_new_key_set_dirty():
    rows = [_variation(SLAB, "var_A"), _variation(BLOCK, "var_B"), _variation(TILE, None, "pending")]
    plan = ms.plan_variations(rows, _key_map(), _survivors())
    assert plan.aborts == {}
    assert list(plan.rows) == [NEW]
    survivor = plan.rows[NEW]
    assert survivor["key"] == NEW and survivor["medusa_id"] == "var_B" and survivor["state"] == "dirty"
    assert plan.collapsed == {NEW: [SLAB, BLOCK, TILE]}


def test_the_survivor_row_has_its_alias_rows_not_computed_and_the_widest_in_full():
    # P2-10 C4: an old row's aliases carry no owner, so no union is carried; alias_rows and codes are NULL (held)
    # until the first produce computes them from the alias stores
    rows = [_variation(SLAB, "var_A", aliases=json.dumps(["Bianco Carrara", "Carrara C"]), in_full=0),
            _variation(BLOCK, "var_B", aliases=json.dumps(["carrara c", "Statuario"]), in_full=0),
            _variation(TILE, None, "pending", aliases=None, in_full=1)]
    survivor = ms.plan_variations(rows, _key_map(), _survivors()).rows[NEW]
    assert (survivor["alias_rows"], survivor["codes"]) == (None, None)
    assert survivor["in_full"] == 1 and survivor["medusa_id"] == "var_B"


def test_a_ghost_with_no_medusa_record_becomes_one_pending_row_from_its_first_copy_by_rank():
    rows = [_variation(G_BLOCK, None, "pending", name="Ghost"), _variation(G_SLAB, None, "pending", name="Ghost")]
    plan = ms.plan_variations(rows, _key_map(), _survivors())
    assert plan.aborts == {}
    assert plan.rows[G_NEW]["state"] == "pending" and plan.rows[G_NEW]["medusa_id"] is None
    assert plan.collapsed[G_NEW] == [G_SLAB, G_BLOCK]                 # the slab (rank 0) copy is the row kept


@pytest.mark.parametrize("bad, kind", [
    (_variation(SLAB, "var_A", "retiring"), "unsafe_state"),
    (_variation(SLAB, "var_A", "gap_held"), "unsafe_state"),
    (_variation(SLAB, "var_A", abandoned_at="2026-11-01T00:00:00"), "unsafe_state"),
    (_variation("slab_marble_unknown_u9", "var_X"), "unmapped_key"),
])
def test_a_copy_the_migration_cannot_decide_aborts_and_is_listed(bad, kind):
    rows = [bad, _variation(BLOCK, "var_B"), _variation(TILE, None, "pending")]
    plan = ms.plan_variations(rows, _key_map(), _survivors())
    assert plan.aborts[kind] == [bad["key"]]


def test_a_survivor_id_held_by_no_copy_aborts():
    rows = [_variation(SLAB, "var_A"), _variation(BLOCK, "var_Z"), _variation(TILE, None, "pending")]
    plan = ms.plan_variations(rows, _key_map(), _survivors())
    assert plan.aborts["survivor_not_in_ledger"] == [NEW]


def test_a_ghost_copy_with_a_medusa_record_aborts():
    rows = [_variation(G_SLAB, "var_G"), _variation(G_BLOCK, None, "pending")]
    plan = ms.plan_variations(rows, _key_map(), _survivors())
    assert plan.aborts["no_survivor_but_synced"] == [G_NEW]


def test_the_plan_is_the_same_whatever_the_row_order():
    rows = [_variation(TILE, None, "pending"), _variation(BLOCK, "var_B"), _variation(SLAB, "var_A"),
            _variation(G_SLAB, None, "pending"), _variation(G_BLOCK, None, "pending")]
    one = ms.plan_variations(rows, _key_map(), _survivors())
    two = ms.plan_variations(list(reversed(rows)), _key_map(), _survivors())
    assert one == two


# --- ledger product rows and tombstones ---------------------------------------------------------------------------
def test_a_product_points_at_its_variety_s_new_key():
    products = [{"sku": "S1", "variation_key": TILE}, {"sku": "S2", "variation_key": None}]
    remapped, aborts = ms.remap_products(products, _key_map())
    assert [p["variation_key"] for p in remapped] == [NEW, None] and aborts == {}
    _, aborts = ms.remap_products([{"sku": "S3", "variation_key": "slab_x_y_u0"}], _key_map())
    assert aborts == {"unmapped_key": ["S3"]}


def test_variation_tombstones_on_old_keys_are_dropped_and_listed_product_tombstones_stay():
    removed = [{"external_id": BLOCK, "kind": "variation", "reason": "retired"},
               {"external_id": "SKU-9", "kind": "product", "reason": "delisted"}]
    kept, listed = ms.drop_variation_tombstones(removed, _key_map())
    assert kept == [removed[1]]                                        # never re-pointed: it would delete the survivor
    assert listed == [{"old_key": BLOCK, "new_key": NEW, "reason": "retired"}]


# --- config.db ---------------------------------------------------------------------------------------------------
def test_a_retirement_of_every_copy_becomes_one_variety_retirement_and_a_partial_one_aborts():
    copies = {SLAB, BLOCK, TILE}
    assert ms.remap_retired({SLAB, BLOCK, TILE}, _key_map(), copies) == ({NEW}, {})
    assert ms.remap_retired({SLAB}, _key_map(), copies) == (set(), {"partial_retirement": [NEW]})


def test_protected_keys_move_to_the_new_key_and_two_on_one_new_key_abort():
    assert ms.remap_protected({SLAB}, _key_map()) == ({NEW}, {})
    assert ms.remap_protected({SLAB, BLOCK}, _key_map()) == (set(), {"protected_collision": [BLOCK, SLAB]})


# --- the learned aliases (alias_writeback.csv) ------------------------------------------------------------------
def test_a_learned_alias_moves_to_the_survivor_once_whichever_copy_learned_it():
    ledger = [_variation(SLAB, "var_A"), _variation(BLOCK, "var_B"), _variation(TILE, None, "pending")]
    records = [("var_A", "Carrara C", "learned"), ("var_B", "carrara c", "learned"), (TILE, "Bianco C", "learned")]
    remapped, aborts = ms.remap_writeback(records, ledger, _key_map(), _survivors())
    assert aborts == {}
    assert remapped == [("var_B", "Bianco C", "learned"), ("var_B", "Carrara C", "learned")]   # sorted, deduped


def test_a_learned_alias_on_an_unknown_id_aborts():
    _, aborts = ms.remap_writeback([("var_Q", "X", "learned")], [_variation(SLAB, "var_A")], _key_map(), _survivors())
    assert aborts == {"unmapped_variation_id": ["var_Q"]}
