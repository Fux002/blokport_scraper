"""VE2 PR5: fan out only to assignable values, keep emitted products' exact tuples, count frozen varieties
frozen not uncovered (tree_build); archived varieties stay in the curate index; no alias onto an archived
Key; a mint drops restricted colours/qualities and holds a restricted type. Dormant (byte-identical) while
attributes.csv / the export carry no status columns.
"""

from __future__ import annotations

import csv

from stone_pipeline.stages import tree_build
from stone_pipeline.tests.test_tree_build import (COL, FIN, _backbone, _export, _for,
                                                  _post, _products, _prow)

# the same vocabulary as test_tree_build._attrs, but written WITH status/internal columns so values can be
# marked archived/internal. sourceids match so backbone/product references resolve.
_ATTR_ROWS = [("category", "Slabs", "cat_slab"), ("category", "Blocks", "cat_block"),
              ("category", "Tiles", "cat_tile"), ("type", "Marble", "t_marble"),
              ("type", "Quartzite", "t_quartz"),
              ("finish", "Polished", "f_pol"), ("finish", "Honed", "f_hon"), ("finish", "Raw", "f_raw"),
              ("color", "White", "c_white"), ("color", "Grey", "c_grey"), ("quality", "A", "q_a")]


def _attrs_status(tmp_path, archived=(), internal=(), name="attributes.csv"):
    p = tmp_path / name
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["category", "value", "sourceid", "status", "internal"])
        for cat, val, sid in _ATTR_ROWS:
            w.writerow([cat, val, sid,
                        "archived" if (cat, val) in archived else "active",
                        "true" if (cat, val) in internal else "false"])
    return p


def _attrs_plain(tmp_path, name="attributes_plain.csv"):
    p = tmp_path / name
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["category", "value", "sourceid"])
        w.writerows(_ATTR_ROWS)
    return p


def _export_status(tmp_path, rows):   # rows: (Id, Key, Name, status)
    p = tmp_path / "export_status.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)", "status"])
        for vid, key, nm, st in rows:
            w.writerow([vid, key, nm, "", "", "", st])
    return p


def _scene(tmp_path):
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished", "Honed"],
                                    color=("White", "Grey"))])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara")])
    return bb, exp


def test_dormant_is_byte_identical(tmp_path):
    # status columns present but nothing restricted == no status columns at all (expand phase)
    bb, exp = _scene(tmp_path)
    prod = _products(tmp_path, [_prow(vid="v1", fin="f_pol", col="c_white")])
    plain, _, _ = tree_build.build_combinations(exp, _attrs_plain(tmp_path), [bb], prod)
    statused, _, _ = tree_build.build_combinations(exp, _attrs_status(tmp_path), [bb], prod)
    assert plain == statused


def test_restricted_finish_drops_from_fanout_but_stays_for_an_emitted_product(tmp_path):
    bb, exp = _scene(tmp_path)
    # an emitted product carries the restricted finish Honed (validate let it emit only because it is acked)
    prod = _products(tmp_path, [_prow(vid="v1", fin="f_hon", col="c_white", qual="q_a")])
    attrs = _attrs_status(tmp_path, archived=[("finish", "Honed")])
    combos, stats, _ = tree_build.build_combinations(exp, attrs, [bb], prod)
    v1 = _for(combos, "v1")
    assert ("cat_slab", "t_marble", "v1", "f_hon", "c_white", "q_a") in v1   # kept: the emitted product tuple
    assert ("cat_slab", "t_marble", "v1", "f_hon", "c_grey", "q_a") not in v1  # fan-out Honed dropped
    assert {c[FIN] for c in v1 if c[COL] == "c_grey"} == {"f_pol"}            # grey priceable only in Polished


def test_a_product_tuple_absent_from_the_fanout_is_not_added(tmp_path):
    bb, exp = _scene(tmp_path)
    # the product asserts a DIFFERENT type (Quartzite) than the variation's Key (Marble). The fan-out pins
    # type to the Key, so the product's exact tuple is never a fan-out tuple -> it must NOT be added (the
    # intersection, not a plain union), and it is reported.
    prod = _products(tmp_path, [_prow(vid="v1", typ="t_quartz", fin="f_pol", col="c_white", qual="q_a")])
    attrs = _attrs_status(tmp_path, archived=[("finish", "Honed")])
    combos, stats, _ = tree_build.build_combinations(exp, attrs, [bb], prod)
    assert ("cat_slab", "t_quartz", "v1", "f_pol", "c_white", "q_a") not in combos   # never fanned -> not added
    assert stats["product_tuples_absent_from_fanout"] >= 1


def test_restricted_category_pcat_drops_the_speculative_fanout_but_keeps_an_emitted_product(tmp_path):
    # a tile variation whose category pcat (Tiles) is archived: its SPECULATIVE fan-out combinations drop, but
    # an emitted product carrying the archived category keeps its exact tuple (validate allows it only acked).
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"], color=("White",)),
                              _post("tile_marble_carrara_1", "Carrara", ["Polished"],
                                    category="Tiles", color=("White", "Grey"))])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara"),
                             ("vt", "tile_marble_carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="v1"), _prow(cat="cat_tile", vid="vt", col="c_white")])
    attrs = _attrs_status(tmp_path, archived=[("category", "Tiles")])
    combos, _, _ = tree_build.build_combinations(exp, attrs, [bb], prod)
    assert _for(combos, "v1")                                                      # the slab still prices
    assert ("cat_tile", "t_marble", "vt", "f_pol", "c_white", "q_a") in combos     # emitted product tuple kept
    assert ("cat_tile", "t_marble", "vt", "f_pol", "c_grey", "q_a") not in combos  # speculative Tiles dropped


def test_a_frozen_variety_is_counted_frozen_not_uncovered(tmp_path):
    # an archived variety with NO colour/type source would be UNCOVERED; frozen, it is counted frozen instead
    bb = _backbone(tmp_path, [])                                 # no backbone post -> no colour/quality
    exp = _export_status(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara", "archived")])
    attrs = _attrs_status(tmp_path)
    combos, stats, uncovered = tree_build.build_combinations(exp, attrs, [bb], None)
    assert stats["frozen"] == 1
    assert not any(u["Key"] == "slab_marble_carrara_1" for u in uncovered)


# --- curate items 2, 3, 4 ---------------------------------------------------

import pytest  # noqa: E402

from stone_pipeline.matching import projections as proj  # noqa: E402
from stone_pipeline.reference import loaders  # noqa: E402
from stone_pipeline.stages import curate  # noqa: E402
from stone_pipeline.stages.curate import minting  # noqa: E402
from stone_pipeline.tests import _decision_views as views  # noqa: E402
from stone_pipeline.tests.test_mint_matrix import _is_held, _minted_keys, _scrape  # noqa: E402


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def test_curate_index_carries_the_archived_flag(tmp_path, monkeypatch):
    # item 2: an archived variety STAYS in the curate index (so a scrape still recognizes it, no re-mint); the
    # index entry carries the export's archived flag. Dormant: False when the export has no status column.
    from stone_pipeline.stages.curate import existing as cur_existing
    p = tmp_path / "export.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)", "status"])
        w.writerow(["v1", "slab_marble_frozen_1", "Frozen", "", "", "", "archived"])
        w.writerow(["v2", "slab_marble_live_1", "Live", "", "", "", "active"])
    monkeypatch.setattr(cur_existing, "existing_varieties_file", lambda: p)
    imports = cur_existing.load_all_existing()
    by_name = imports["slab"].by_name
    assert by_name[proj.norm("Frozen")]["archived"] is True    # stays indexed, flagged
    assert by_name[proj.norm("Live")]["archived"] is False


def test_aliasing_skips_an_archived_variety():
    # item 4: no alias row is written onto an archived variety; an active one still gets its alias.
    from types import SimpleNamespace

    from stone_pipeline.stages.curate.aliasing import emit_alias_rows
    from stone_pipeline.stages.curate.context import CurationResult
    from stone_pipeline.stages.curate.keys import active_branches
    branches = active_branches()
    b = branches[0]
    arch = {"Key": f"{b}_marble_frozen_1", "Name": "Frozen", "Image": "", "Aliases": "", "Volume": "",
            "type": proj.norm("marble"), "archived": True}
    live = {"Key": f"{b}_marble_live_1", "Name": "Live", "Image": "", "Aliases": "", "Volume": "",
            "type": proj.norm("marble"), "archived": False}

    def _imp(branch):
        bynt, byn = {}, {}
        if branch == b:
            for v in (arch, live):
                bynt[(proj.norm(v["Name"]), proj.norm("marble"))] = v
                byn[proj.norm(v["Name"])] = v
        return SimpleNamespace(by_name_type=bynt, by_name=byn)

    c = SimpleNamespace(
        imports={br: _imp(br) for br in branches},
        alias_new={(proj.norm("Frozen"), proj.norm("marble")): {"Froze"},
                   (proj.norm("Live"), proj.norm("marble")): {"Liv"}},
        review_candidates={})
    result = CurationResult()
    result.alias_additions = {br: [] for br in branches}
    emit_alias_rows(c, result)
    keys = {row["Key"] for rows in result.alias_additions.values() for row in rows}
    assert live["Key"] in keys           # the active variety gets its alias
    assert arch["Key"] not in keys       # the archived variety is skipped


def test_mint_with_a_restricted_type_holds(ref, monkeypatch):
    # item 3: a mint whose stone type is restricted HOLDS via the existing pending_confirm path, not minted.
    views.mint("Novum Frozen Stone", stone_type="Granite")   # operator confirms the mint
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"type": {proj.norm("Granite")}})
    res = curate.build_curation([_scrape("Novum Frozen Stone", raw_type="Granite")], ref)
    assert _is_held(res, "Novum Frozen Stone")               # held, surfaced for the operator
    assert not _minted_keys(res, "novum_frozen_stone")       # NOT minted under the archived type


def test_mint_drops_a_restricted_colour(ref, monkeypatch):
    views.mint("Novum Pink Stone", stone_type="Granite")

    def _mk(color):
        r = _scrape("Novum Pink Stone", raw_type="Granite")
        r.color_name = color
        return r

    def _posts(res):
        return [p for b in ("slab", "block", "tile")
                for p in res.backbone_new[b] if "novum_pink_stone" in p["key"]]

    base = _posts(curate.build_curation([_mk("Pink")], ref))
    assert base and any("Pink" in p["color"] for p in base)   # confirms the observed colour flows into the mint
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"color": {proj.norm("Pink")}})
    dropped = _posts(curate.build_curation([_mk("Pink")], ref))
    assert dropped and all("Pink" not in p["color"] for p in dropped)   # restricted colour dropped from the mint
