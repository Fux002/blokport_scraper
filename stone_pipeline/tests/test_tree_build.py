"""Valid-combination build. Every imported variation is priceable; the output is the
flat 6-tuple set (category, type, variation, finish, color, quality) for the relational
`valid_combination` table. Phase 1: a variety is ONE record per (type, name) with no
category, so each one is priced in EVERY fan-out category under its one id, with that
category's finishes (the default form's: its variety posts' and products'; block's: the
pack's declared set; tile's: slab's) x the colour(s) and qualities we know. The type is
the Key's type slug, else its own post's, else the operator's assignment; colour and
quality come from its products (any category), its post, or the operator's decisions.
"""

from __future__ import annotations

import csv
import json

from stone_pipeline.config.categories import category
from stone_pipeline.core.text import match_key
from stone_pipeline.stages import tree_build

_PCOLS = ["Product Category 1", "STN Type Id", "STN Variation Id",
          "STN Finish Id", "STN Color Id", "STN Quality Id"]
# combination tuple positions
CAT, TYPE, VAR, FIN, COL, QUAL = range(6)
_FINISH_IDS = {"Polished": "f_pol", "Honed": "f_hon", "Raw": "f_raw"}
BLOCK_FINISH_IDS = {_FINISH_IDS.get(f, f"f_{match_key(f)}") for f in category("block").finishes}
ALL_CATS = {"cat_slab", "cat_block", "cat_tile"}


def _attrs(tmp_path):
    p = tmp_path / "attributes.csv"
    rows = [("category", "Slabs", "cat_slab"), ("category", "Blocks", "cat_block"),
            ("category", "Tiles", "cat_tile"), ("type", "Marble", "t_marble"),
            ("type", "Quartzite", "t_quartz"),
            ("color", "White", "c_white"), ("color", "Grey", "c_grey"), ("quality", "A", "q_a")]
    finishes = set(_FINISH_IDS) | set(category("block").finishes)    # the vocabulary holds every declared finish
    rows += [("finish", f, _FINISH_IDS.get(f, f"f_{match_key(f)}")) for f in sorted(finishes)]
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
        w.writerow(["Id", "Key", "Name", "Type", "Aliases"])
        for vid, key, nm in rows:
            w.writerow([vid, key, nm, "Marble", ""])
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


def _post(key, variant, finishes, stone_type="Marble", color=("White",), qualities=("A",)):
    return {"key": key, "variant": variant, "stone_type": stone_type,
            "finishes": list(finishes), "color": list(color), "qualities": list(qualities)}


def _for(combos, vid, cat=None):
    return [c for c in combos if c[VAR] == vid and (cat is None or c[CAT] == cat)]


def test_one_variety_is_priced_in_every_category_with_that_category_s_finishes(tmp_path):
    # the default form: its posts' finishes (Polished, Raw) UNION what its products use (Honed); tile takes
    # slab's; block takes the pack's declared set. An UN-SOLD variation is priceable in every one of them.
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished", "Raw"])])
    exp = _export(tmp_path, [("v1", "marble__carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="sold1", fin="f_pol"), _prow(vid="sold2", fin="f_hon")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert {c[CAT] for c in _for(combos, "v1")} == ALL_CATS
    assert {c[FIN] for c in _for(combos, "v1", "cat_slab")} == {"f_pol", "f_raw", "f_hon"}
    assert {c[FIN] for c in _for(combos, "v1", "cat_tile")} == {"f_pol", "f_raw", "f_hon"}
    assert {c[FIN] for c in _for(combos, "v1", "cat_block")} == BLOCK_FINISH_IDS
    assert not unc and stats["covered"] == 1


def test_retired_key_gets_no_combinations(tmp_path):
    # T3-4: a retired variety is excluded by its stable KEY, not the churn-prone export Id. Retired Keys
    # are a DISTINCT namespace from exclude_ids (Ids); the export Id differs from the Key (as in Medusa).
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"]),
                              _post("marble__statuario_1", "Statuario", ["Polished"])])
    exp = _export(tmp_path, [("v_id_1", "marble__carrara_1", "Carrara"),
                             ("v_id_2", "marble__statuario_1", "Statuario")])
    prod = _products(tmp_path, [_prow()])
    combos, _, _ = tree_build.build_combinations(
        exp, _attrs(tmp_path), [bb], prod, retired_keys={"marble__statuario_1"})
    assert _for(combos, "v_id_1")          # Carrara still priced
    assert not _for(combos, "v_id_2")      # retired Statuario gets NO combinations


def test_retired_key_wrongly_placed_in_the_id_set_does_not_exclude(tmp_path):
    # Guards the exact bug: a retired KEY folded into exclude_ids (the Id namespace) never matches the
    # export Id, so the variety keeps every combination -- which is why retired must be its own Key set.
    bb = _backbone(tmp_path, [_post("marble__statuario_1", "Statuario", ["Polished"])])
    exp = _export(tmp_path, [("v_id_2", "marble__statuario_1", "Statuario")])
    prod = _products(tmp_path, [_prow()])
    combos, _, _ = tree_build.build_combinations(
        exp, _attrs(tmp_path), [bb], prod, exclude_ids={"marble__statuario_1"})
    assert _for(combos, "v_id_2")          # NOT excluded: the Key-in-Id-set was inert (the old bug)


def test_combination_tuple_shape_and_csv(tmp_path):
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("v1", "marble__carrara_1", "Carrara")])
    combos, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], _products(tmp_path, [_prow()]))
    assert combos and all(len(c) == 6 for c in combos)
    # every field is a string id -- run() relies on this to reuse the built set as the CSV-diff baseline
    # WITHOUT re-copying 2M rows (the produce memory optimisation). If this ever regresses, run()'s
    # defensive normalisation still keeps it correct, but the common no-copy path depends on it.
    assert all(isinstance(x, str) for c in combos for x in c)
    out = tmp_path / "combos.csv"
    n = tree_build.write_combinations(combos, out)
    rows = list(csv.reader(out.open(encoding="utf-8-sig")))
    assert rows[0] == list(tree_build.COMBINATION_COLUMNS)
    assert n == len(combos) == len(rows) - 1


def test_product_backed_unions_backbone_and_scraped_colour(tmp_path):
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"], color=["White"])])
    exp = _export(tmp_path, [("v1", "marble__carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="v1", col="c_grey")])  # scraped Grey, backbone White
    combos, stats, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert {c[COL] for c in _for(combos, "v1")} == {"c_white", "c_grey"}  # both -- widest valid set
    assert stats["covered"] == 1


def test_a_colour_sold_in_one_category_prices_the_variety_in_every_category(tmp_path):
    # the variety sold in Grey as a block: it is ONE record, so Grey is valid for its slabs and tiles too
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("v1", "marble__carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(cat="cat_block", vid="v1", fin="f_raw", col="c_grey")])
    combos, _, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert not unc
    for cat in ALL_CATS:
        assert {c[COL] for c in _for(combos, "v1", cat)} == {"c_white", "c_grey"}


def test_unbackboned_variation_with_no_key_type_is_uncovered_with_its_reasons(tmp_path):
    # no product, no backbone, no known type slug in the Key: nothing states its type, colour or quality.
    # It used to be typed from its Name and priced under the catalogue's default colour (wave 3c).
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("vs", "marble__carrara_1", "Carrara"),
                             ("vq", "everest__quartzite_2", "Everest Quartzite")])
    prod = _products(tmp_path, [_prow(vid="vs", col="c_grey")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert not _for(combos, "vq")
    assert [(u["Id"], u["missing"]) for u in unc] == [("vq", "type+colour+quality")]


def test_truly_unresolvable_variation_is_uncovered(tmp_path):
    # type not derivable from the Key, its post or the operator -> can't be placed
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("vs", "marble__carrara_1", "Carrara"),
                             ("vm", "zzz__mystery_2", "Mystery")])
    prod = _products(tmp_path, [_prow(vid="vs")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert [u["Id"] for u in unc] == ["vm"]
    assert not _for(combos, "vm")


def test_build_is_deterministic(tmp_path):
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"])])
    exp = _export(tmp_path, [("v1", "marble__carrara_1", "Carrara")])
    prod = _products(tmp_path, [_prow(vid="v1")])
    a, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    b, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert a == b and sorted(a) == sorted(b)  # same set, deterministic row order


def test_partial_export_row_is_warned_not_silently_dropped(tmp_path, caplog):
    # A row carrying data but MISSING Id/Key is a real variation dropped from combinations -- it must be
    # surfaced (fail-loud), while a blank line and a thin/header-only cold-start export stay silent and
    # never abort. Distinguishes the two.
    import logging
    bb = _backbone(tmp_path, [_post("marble__carrara_1", "Carrara", ["Polished"])])
    exp = tmp_path / "export.csv"
    with exp.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", "Key", "Name", "Type", "Aliases"])
        w.writerow(["v1", "marble__carrara_1", "Carrara", "Marble", ""])
        w.writerow(["", "", "Orphan Marble", "Marble", ""])   # data present, no Id/Key -> warned
        w.writerow(["", "", "", "", ""])                      # blank line -> silent
    with caplog.at_level(logging.WARNING):
        combos, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], _products(tmp_path, [_prow()]))
    assert _for(combos, "v1")                               # the good row still built, no abort
    warns = [r for r in caplog.records if "missing Id/Key" in r.getMessage()]
    assert len(warns) == 1                                  # the partial row surfaced, blank line ignored


def test_run_missing_export_writes_empty_not_abort(tmp_path, monkeypatch):
    # COLD START: Medusa has 0 variations, so variants_export.csv doesn't exist yet. run() must NOT abort
    # (it used to raise SystemExit) -- it writes empty combination sets so pass 1 completes; the real
    # export arrives after Medusa mints this produce's variants, and the next republish builds combos.
    import types
    fake = types.SimpleNamespace(paths=types.SimpleNamespace(
        export_file=tmp_path / "does-not-exist.csv", to_upload_dir=tmp_path))
    monkeypatch.setattr(tree_build, "SETTINGS", fake)
    ret = tree_build.run()                                  # must not raise
    assert ret == tmp_path / "2_valid_combinations.csv"
    for name in ("2_valid_combinations.csv", "2_valid_combinations_products_only.csv"):
        rows = list(csv.reader((tmp_path / name).open(encoding="utf-8-sig")))
        assert rows == [list(tree_build.COMBINATION_COLUMNS)]    # header only, zero data rows
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2_valid_combinations.csv",
                                                           "2_valid_combinations_products_only.csv"]


def test_run_writes_the_full_set_and_no_delta(tmp_path, monkeypatch):
    # Blokport's import always replaces, keyed on the manifest's sha256 of the FULL set, so the build writes
    # exactly the full set (plus the testing one-off) and no "new since the last build" delta. A previous
    # build's file is overwritten, never diffed against.
    import types
    from stone_pipeline.config import decisions_store
    from stone_pipeline.stages import decisions
    export = tmp_path / "variants_export.csv"
    export.write_text("Id\n", encoding="utf-8")
    attrs = tmp_path / "attributes.csv"
    attrs.write_text("category,value,sourceid\n", encoding="utf-8")
    to_upload, review = tmp_path / "to_upload", tmp_path / "review"
    to_upload.mkdir(); review.mkdir()
    tree_build.write_combinations({("c", "t", "old", "f", "k", "q")}, to_upload / "2_valid_combinations.csv")
    fake = types.SimpleNamespace(paths=types.SimpleNamespace(
        export_file=export, to_upload_dir=to_upload, review_dir=review, attributes_csv=attrs,
        backbone_json=tmp_path / "catalog_source" / "backbone_varieties.json",
        backbone_additions_file=tmp_path / "catalog_source" / "backbone_additions" / "varieties.json"))
    monkeypatch.setattr(tree_build, "SETTINGS", fake)
    monkeypatch.setattr(decisions, "load_retired", lambda: set())
    monkeypatch.setattr(decisions_store, "backbone_leaf_overlay", lambda: {})
    built = {("c", "t", "v1", "f", "k", "q")}
    monkeypatch.setattr(tree_build, "build_combinations", lambda *a, **k: (built, {}, []))
    assert tree_build.run() == to_upload / "2_valid_combinations.csv"
    rows = list(csv.reader((to_upload / "2_valid_combinations.csv").open(encoding="utf-8-sig")))
    assert rows == [list(tree_build.COMBINATION_COLUMNS), ["c", "t", "v1", "f", "k", "q"]]
    assert sorted(p.name for p in to_upload.iterdir()) == ["2_valid_combinations.csv",
                                                            "2_valid_combinations_products_only.csv"]


# --- wave 3c: no value is guessed into a combination ---------------------------------------------------

def test_a_variation_nothing_describes_is_uncovered_not_priced_under_the_common_colour(tmp_path):
    # v1 is described (product row: White / A); v2 has NO product, NO backbone post: nothing says its colour
    # or quality. It used to inherit the catalogue's most common colour and quality as a "last resort".
    bb = _backbone(tmp_path, [_post("marble__alpha_1", "Alpha", ["Polished"])])
    exp = _export(tmp_path, [("v1", "marble__alpha_1", "Alpha"), ("v2", "marble__beta_2", "Beta")])
    prod = _products(tmp_path, [_prow(vid="v1")])
    combos, stats, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], prod)
    assert _for(combos, "v1")
    assert not _for(combos, "v2")
    assert [u["Id"] for u in unc] == ["v2"]
    assert unc[0]["missing"] == "colour+quality"          # the review file says what the operator must supply
    assert stats["uncovered"] == 1


def test_a_same_name_post_of_another_type_lends_no_colour(tmp_path):
    # a QUARTZITE 'Gamma' post (Grey); the MARBLE 'Gamma' has no post of its own and no product. A name-only
    # join would give the marble the quartzite's colour: a different stone. It is uncovered instead.
    bb = _backbone(tmp_path, [_post("quartzite__gamma_9", "Gamma", ["Raw"], stone_type="Quartzite", color=["Grey"])])
    exp = _export(tmp_path, [("v1", "marble__gamma_1", "Gamma")])
    combos, _, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [bb], None)
    assert not _for(combos, "v1")
    assert [u["Id"] for u in unc] == ["v1"]


def test_a_type_word_in_the_name_is_not_the_type(tmp_path):
    # Key carries no known type slug, no post, no operator assignment: 'Everest Quartzite' used to be typed
    # Quartzite from its NAME. Type comes from the Key, the Key-matched post or the operator, never a guess.
    exp = _export(tmp_path, [("v1", "unknownstone__everest_quartzite_1", "Everest Quartzite")])
    prod = _products(tmp_path, [_prow(vid="v1", typ="")])
    combos, _, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [], prod)
    assert not _for(combos, "v1")
    assert [u["Id"] for u in unc] == ["v1"]
    assigned = tree_build.build_combinations(exp, _attrs(tmp_path), [], prod, {"everest quartzite": "t_quartz"})[0]
    assert {c[1] for c in _for(assigned, "v1")} == {"t_quartz"}     # the operator's assignment covers it


def test_leaf_overlay_covers_a_decided_variety(tmp_path):
    # An operator-APPROVED colour/quality (config.db leaf overlay) makes a variety priceable even when it is
    # minted-and-drifted out of the backbone files (no post) with no product -- otherwise it is re-flagged
    # UNCOVERED although the decision already exists (the bug: tree_build read the files, not the overlay
    # reference_data grows the matching backbone with). Type-scoped: a same-name different stone must not
    # inherit it.
    exp = _export(tmp_path, [
        ("b1", "marble__crystal_vitro_1", "Crystal Vitro"),     # no post, no product
        ("b2", "quartzite__crystal_vitro_1", "Crystal Vitro"),  # SAME name, DIFFERENT type
    ])
    overlay = {("crystal vitro", "marble"): {"color": ["White"], "quality": ["A"]}}
    # without the overlay -> uncovered
    combos0, _, _ = tree_build.build_combinations(exp, _attrs(tmp_path), [], None)
    assert not _for(combos0, "b1")
    # with the overlay -> the marble one is covered from the decision; the quartzite same-name is NOT
    combos, _, unc = tree_build.build_combinations(exp, _attrs(tmp_path), [], None, leaf_overlay=overlay)
    b1 = _for(combos, "b1")
    assert b1, "a decided variety must be covered by the leaf overlay"
    assert {c[COL] for c in b1} == {"c_white"}
    assert {c[QUAL] for c in b1} == {"q_a"}
    assert not _for(combos, "b2")                       # type-scoped: quartzite same-name not covered
    assert "b2" in {u["Id"] for u in unc}
