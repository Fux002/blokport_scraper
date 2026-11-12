"""SC-4: the committed variety data on phase 1's Keys (plan SC-3-SC-4-PLAN.md sections 1b and 2g). The rewrite maps
every committed old Key through SC-1's key map (never re-minting), collapses an identity's copies to ONE row, and
is idempotent. The committed files themselves carry no branch-prefixed Key (the definition-of-done grep, spec
section 8), in the four variety-file columns.
"""

from __future__ import annotations

import csv
import hashlib

import pytest

from stone_pipeline.phase1 import rewrite_committed
from stone_pipeline.phase1.migrate_stores import read_key_map
from stone_pipeline.phase1.variety_key import variety_key_problem
from stone_pipeline.reference.medusa_files import VARIETY_FILE_COLUMNS

_NEW = "granite__absolute_black_7f3c0b6e-1111-5222-8333-944455556666"
_MAP = ("old_key,new_key,category_rank,type_name\n"
        f"slab_granite_absolute_black_a,{_NEW},0,Granite\n"
        f"block_granite_absolute_black_b,{_NEW},1,Granite\n"
        f"tile_granite_absolute_black_c,{_NEW},2,Granite\n").encode()
_OLD_BASE = ("Key,Name,Image,Aliases,Volume per kg (m³/kg)\r\n"
             "block_granite_absolute_black_b,Absolute Black Block,https://x/b.png,black absolute|Nero Assoluto,0.0014\r\n"
             "tile_granite_absolute_black_c,Absolute Black,,Tile Black,\r\n"
             "slab_granite_absolute_black_a,Absolute Black,https://x/a.png,Black Absolute|Shanxi Black,0.00037\r\n")


@pytest.fixture
def key_map():
    return read_key_map(_MAP, hashlib.sha256(_MAP).hexdigest())


def test_an_identitys_copies_collapse_to_one_row_on_its_new_key(key_map):
    out = rewrite_committed.rewrite_base(_OLD_BASE, key_map, "base")
    assert list(csv.DictReader(out.splitlines())) == [
        {"Key": _NEW, "Name": "Absolute Black", "Type": "Granite",
         "Aliases": "Black Absolute|Shanxi Black|Nero Assoluto|Tile Black"}]   # the slab copy's casing first


def test_the_rewrite_is_idempotent(key_map):
    once = rewrite_committed.rewrite_base(_OLD_BASE, key_map, "base")
    assert rewrite_committed.rewrite_base(once, key_map, "base") == once


def test_a_key_outside_the_map_refuses_naming_it(key_map):
    with pytest.raises(rewrite_committed.UnmappedKey, match="slab_marble_unknown"):
        rewrite_committed.rewrite_base("Key,Name,Image,Aliases\r\nslab_marble_unknown,X,,\r\n", key_map, "base")


@pytest.mark.parametrize("path", rewrite_committed.BASE_FILES, ids=lambda p: p.parent.name)
def test_the_committed_bases_are_on_phase1_keys(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == VARIETY_FILE_COLUMNS
    assert rows and not [r["Key"] for r in rows if variety_key_problem(r["Key"])]
    assert len({r["Key"] for r in rows}) == len(rows) and all(r["Type"] for r in rows)
