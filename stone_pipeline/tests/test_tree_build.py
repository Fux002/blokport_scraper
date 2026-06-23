"""Valid-combination build. Every imported variation is priceable; the output is the
flat 6-tuple set (category, type, variation, finish, color, quality) for the relational
`valid_combination` table. Each variation gets its category's PRODUCT-USED finishes
(tiles mirror slabs) x the colour(s) we know; colour/type come from the best source:
the product that sold it, its backbone post, a same-variety product in another category
(fan-out mirrors), or the type parsed from the Key/Name + the catalogue default colour.
"""

from __future__ import annotations

import csv
import json

from stone_pipeline.stages import tree_build

_PCOLS = ["Product Category 1", "STN Type Id", "STN Variation Id",
          "STN Finish Id", "STN Color Id", "STN Quality Id"]
# combination tuple positions
CAT, TYPE, VAR, FIN, COL, QUAL = range(6)


def _attrs(tmp_path):
    p = tmp_path / "attributes.csv"
    rows = [("category", "Slabs", "cat_slab"), ("category", "Blocks", "cat_block"),
            ("category", "Tiles", "cat_tile"), ("type", "Marble", "t_marble"),
            ("type", "Quartzite", "t_quartz"),
            ("finish", "Polished", "f_pol"), ("finish", "Honed", "f_hon"), ("finish", "Raw", "f_raw"),
            ("color", "White", "c_white"), ("color", "Grey", "c_grey"), ("quality", "A", "q_a")]
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["category", "value", "sourceid"])
        w.writerows(rows)
    return p


def _backbone(tmp_path, posts, name="backbone.json"):
    p = tmp_path / name
    p.write_text(json.dumps({"posts": posts}), encoding="utf-8")
    return p


def _export(tmp_path, rows):  # rows: (Id, Key, Name)
    p = tmp_path / "export.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"])
        for vid, key, nm in rows:
            w.writerow([vid, key, nm, "", "", ""])
    return p


def _products(tmp_path, rows):
    p = tmp_path / "products.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=_PCOLS)
        w.writeheader()
        w.writerows(rows)
    return p


def _prow(cat="cat_slab", typ="t_marble", vid="v1", fin="f_pol", col="c_white", qual="q_a"):
    return dict(zip(_PCOLS, [cat, typ, vid, fin, col, qual]))


def _post(key, variant, finishes, category="Slabs", stone_type="Marble",
          color=("White",), qualities=("A",)):
    return {"key": key, "variant": variant, "category": category, "stone_type": stone_type,
            "finishes": list(finishes), "color": list(color), "qualities": list(qualities)}


def _for(combos, vid):
    return [c for c in combos if c[VAR] == vid]


def test_every_variation_gets_all_category_finishes(tmp_path):
    # category finishes = ALL the backbone's finishes (Polished, Raw) UNION any its
    # products use (Honed). An UN-SOLD variation is priceable in every one of them.
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished", "Raw"])])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="sold1", fin="f_pol"), _prow(vid="sold2", fin="f_hon")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert {c[FIN] for c in _for(combos, "v1")} == {"f_pol", "f_raw", "f_hon"}
    assert not unc


def test_combination_tuple_shape_and_csv(tmp_path):
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara")])
    combos, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], _products(tmp_path, [_prow()]))
    assert combos and all(len(c) == 6 for c in combos)
    out = tmp_path / "combos.csv"
    n = tree_build.write_combinations(combos, out)
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    assert rows[0] == list(tree_build.COMBINATION_COLUMNS)
    assert n == len(combos) == len(rows) - 1


def test_product_backed_unions_backbone_and_scraped_colour(tmp_path):
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"], color=["White"])])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="v1", col="c_grey")])  # scraped Grey, backbone White
    combos, stats, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert {c[COL] for c in _for(combos, "v1")} == {"c_white", "c_grey"}  # both — widest valid set
    assert stats["covered"] == 1


def test_tiles_mirror_slab_finishes(tmp_path):
    # slabs sold in Polished+Honed; a tile (no tile products) is priceable in BOTH
    bb = _backbone(tmp_path, [_post("tile_marble_carrara_1", "Carrara", ["Polished"], category="Tiles")])
    exp = _export(tmp_path, [("vt", "tile_marble_carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="s1", fin="f_pol"), _prow(vid="s2", fin="f_hon")])
    combos, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert {c[FIN] for c in _for(combos, "vt")} == {"f_pol", "f_hon"}


def test_mirror_inherits_colour_from_variety_sold_elsewhere(tmp_path):
    # a slab variety sold in Grey; its block mirror (no block product/backbone) inherits Grey
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"]),
                              _post("block_marble_x_9", "X", ["Raw"], category="Blocks")])
    exp = _export(tmp_path, [("vs", "slab_marble_carrara_1", "Carrara"),
                             ("vb", "block_marble_carrara_2", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="vs", col="c_grey")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert not unc
    blk = _for(combos, "vb")
    assert {c[CAT] for c in blk} == {"cat_block"}      # category is the variation's own
    assert {c[FIN] for c in blk} == {"f_raw"}          # block finishes, not slab
    assert {c[COL] for c in blk} == {"c_white", "c_grey"}  # backbone White + inherited scraped Grey


def test_unbackboned_variation_priceable_via_type_from_name(tmp_path):
    # no product, no backbone -> type parsed from the Name ('Everest Quartzite' -> Quartzite),
    # colour from the catalogue default, so it's still priceable
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("vs", "slab_marble_carrara_1", "Carrara"),
                             ("vq", "slab_everest_quartzite_2", "Everest Quartzite")])
    prod = _products(tmp_path, [_prow(vid="vs", col="c_grey")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert not unc
    assert _for(combos, "vq")  # priceable via type-from-name + default colour


def test_truly_unresolvable_variation_is_uncovered(tmp_path):
    # type not derivable from Key or Name -> can't be placed
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("vs", "slab_marble_carrara_1", "Carrara"),
                             ("vm", "slab_zzz_mystery_2", "Mystery")])
    prod = _products(tmp_path, [_prow(vid="vs")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert [u["Id"] for u in unc] == ["vm"]
    assert not _for(combos, "vm")


def test_build_is_deterministic(tmp_path):
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="v1")])
    a, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    b, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert a == b and sorted(a) == sorted(b)  # same set, deterministic row order
