"""Combinations inherit only from the SAME (type, name), and the update delta never goes stale (owner, 2026-11-06).

  * tree_build inherited colours/qualities from products keyed by variety NAME only, so one type's product
    colours leaked onto a same-name variety of another type (Gamma Quartzite sold Grey -> Gamma Marble Grey);
    the same-category backbone post was also looked up by name only.
  * curate's update delta was written only when non-empty, but emit_catalog reads it as THIS run's delta, so in
    a long-lived task a previous run's delta was re-applied. Since W26 the delta is curate_delta.csv (emit
    publishes 1_variants_update.csv from it).
  (The multi-country origin hold is rule d in test_origin_multi_country.py.)
Hermetic.
"""
from __future__ import annotations

import csv
import dataclasses

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.stages import curate, decisions, tree_build
from stone_pipeline.stages.curate import CurationResult
from stone_pipeline.tests.test_tree_build import COL, _attrs, _backbone, _export, _for, _post, _products, _prow


# --- combinations: (type, name) inheritance ----------------------------------------------------------------
def test_a_product_colour_never_leaks_onto_a_same_name_variety_of_another_type(tmp_path):
    bb = _backbone(tmp_path, [_post("slab_marble_gamma_1", "Gamma", ["Polished"], color=["White"]),
                              _post("slab_quartzite_gamma_2", "Gamma", ["Polished"], stone_type="Quartzite",
                                    color=["White"])])
    exp = _export(tmp_path, [("vm", "slab_marble_gamma_1", "Gamma"), ("vq", "slab_quartzite_gamma_2", "Gamma")])
    prod = _products(tmp_path, [_prow(vid="vq", typ="t_quartz", col="c_grey")])
    combos, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert {c[COL] for c in _for(combos, "vq")} == {"c_white", "c_grey"}
    assert {c[COL] for c in _for(combos, "vm")} == {"c_white"}


def test_a_same_type_product_in_another_category_still_lends_its_colour(tmp_path):
    bb = _backbone(tmp_path, [_post("slab_quartzite_gamma_1", "Gamma", ["Polished"], stone_type="Quartzite",
                                    color=["White"]),
                              _post("block_quartzite_x_9", "X", ["Raw"], category="Blocks", stone_type="Quartzite")])
    exp = _export(tmp_path, [("vs", "slab_quartzite_gamma_1", "Gamma"), ("vb", "block_quartzite_gamma_2", "Gamma")])
    prod = _products(tmp_path, [_prow(vid="vs", typ="t_quartz", col="c_grey")])
    combos, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert {c[COL] for c in _for(combos, "vb")} == {"c_white", "c_grey"}


def test_a_keyless_same_category_post_of_another_type_lends_nothing(tmp_path):
    # the block backbone is keyless: both posts are found by name. The marble post must not colour the
    # quartzite variation; the quartzite post does.
    bb = _backbone(tmp_path, [_post("", "Gamma", ["Raw"], category="Blocks", color=["White"]),
                              _post("", "Gamma", ["Raw"], category="Blocks", stone_type="Quartzite",
                                    color=["Grey"])])
    exp = _export(tmp_path, [("vm", "block_marble_gamma_1", "Gamma"), ("vq", "block_quartzite_gamma_2", "Gamma")])
    combos, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], _products(tmp_path, []))
    assert {c[COL] for c in _for(combos, "vq")} == {"c_grey"}
    assert {c[COL] for c in _for(combos, "vm")} == {"c_white"}


# --- the update delta is always this run's ---------------------------------------------------------------
def test_an_empty_update_delta_replaces_a_stale_one(tmp_path, monkeypatch):
    dirs = {a: tmp_path / a for a in ("to_upload_dir", "catalog_source_dir", "review_dir")}
    for d in dirs.values():
        d.mkdir(parents=True)
    stale = tmp_path / "outputs" / "curate_delta.csv"
    stale.parent.mkdir()
    monkeypatch.setattr(curate.output, "SETTINGS",
                        dataclasses.replace(SETTINGS, paths=dataclasses.replace(SETTINGS.paths, **dirs,
                                                                                curate_delta_csv=stale)))
    monkeypatch.setattr(decisions, "write_backbone_leaf_pending", lambda *a, **k: 0)
    stale.write_text("Key,Name,Image,Aliases,Volume per kg (m³/kg)\r\nslab_marble_old_1,Old,,,\r\n", encoding="utf-8")
    empty = CurationResult()
    curate.write_curation(empty, [])
    with stale.open(encoding="utf-8-sig", newline="") as h:
        rows = list(csv.reader(h))
    assert rows == [["Key", "Name", "Type", "Aliases"]]
