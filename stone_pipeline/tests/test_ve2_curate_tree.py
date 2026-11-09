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


def test_a_variety_left_with_no_combination_after_the_filter_is_uncovered(tmp_path):
    # 6b: covered is counted AFTER the restricted filter -- a variety whose only colour is restricted, with no
    # emitted/acked product keeping a tuple, ends uncovered (not covered).
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"], color=("White",))])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara")])
    attrs = _attrs_status(tmp_path, archived=[("color", "White")])
    combos, stats, unc = tree_build.build_combinations(exp, attrs, [bb], None)   # no product to keep a tuple
    assert not _for(combos, "v1")                            # every combo dropped as restricted
    assert any(u["Id"] == "v1" for u in unc) and stats["covered"] == 0


def test_an_acked_product_absent_from_this_runs_emit_keeps_its_restricted_tuple(tmp_path, monkeypatch):
    # fix 3: the keep-set is emitted UNION ledger-acked tuples. A product live in Medusa on a restricted value
    # whose source emitted NOTHING this run still keeps its combination (Blokport replaces the whole set).
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger, now_iso
    key = "slab_marble_carrara_1"
    lpath = tmp_path / "dev.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(lpath))
    with Ledger.open(lpath, env=writethrough.ENV_NAME,
                     backend_id_fingerprint=writethrough.backend_fingerprint()) as lg:
        now = now_iso()
        lg.upsert("variation", {"key": key, "branch": "slab", "type": "Marble", "name": "Carrara",
                                "aliases": "[]", "image_url": "", "image_sha256": None, "image_model": None,
                                "volume": "", "medusa_id": "V", "in_full": 1, "payload_hash": "",
                                "state": "synced", "archived": 0, "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))
        lg.upsert("product", {"sku": "ZUCCHI-1", "source": "zucchi", "surrogate_key": "1", "color": "White",
                              "finish": "Polished", "quality": "A", "type": "Marble", "category": "Slabs",
                              "variation_key": key, "medusa_id": "M1", "state": "synced",
                              "created_at": now, "updated_at": now}, pk=("sku",))
    bb = _backbone(tmp_path, [_post(key, "Carrara", ["Polished"], color=("White",))])
    exp = _export(tmp_path, [("v1", key, "Carrara")])
    attrs = _attrs_status(tmp_path, archived=[("color", "White")])
    combos, _, _ = tree_build.build_combinations(exp, attrs, [bb], None)   # NO products emitted this run
    assert ("cat_slab", "t_marble", "v1", "f_pol", "c_white", "q_a") in combos   # kept via the acked tuple


# --- curate items 2, 3, 4 ---------------------------------------------------

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from stone_pipeline.matching import projections as proj  # noqa: E402
from stone_pipeline.reference import loaders  # noqa: E402
from stone_pipeline.stages import curate  # noqa: E402
from stone_pipeline.stages.curate import existing as cur_existing  # noqa: E402
from stone_pipeline.stages.curate import minting  # noqa: E402
from stone_pipeline.tests import _decision_views as views  # noqa: E402
from stone_pipeline.tests.test_mint_matrix import _is_held, _minted_keys, _scrape  # noqa: E402


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _mk_row(name, color, sk="s1"):
    r = _scrape(name, raw_type="Granite", sk=sk)
    r.color_name = color
    return r


def _posts(res, slug):
    return [p for b in ("slab", "block", "tile") for p in res.backbone_new[b] if slug in p["key"]]


def test_mint_with_a_restricted_type_holds(ref, monkeypatch):
    # item 3 / fix 1: a genuinely NEW variety whose stone type is restricted HOLDS via the existing
    # pending_confirm path, not minted.
    views.mint("Novum Frozen Stone", stone_type="Granite")   # operator confirms the mint
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"type": {proj.norm("Granite")}})
    res = curate.build_curation([_scrape("Novum Frozen Stone", raw_type="Granite")], ref)
    assert _is_held(res, "Novum Frozen Stone")               # held, surfaced for the operator
    assert not _minted_keys(res, "novum_frozen_stone")       # NOT minted under the archived type


def test_an_existing_variety_of_a_restricted_type_gives_no_card_or_mint(ref, monkeypatch):
    # fix 1 (protected): the restricted-type hold must NOT fire for a variety that already exists (it re-enters
    # emit_new_variants every produce); a card there would flood the queue and following it would mint a
    # duplicate under a wrong type. Use a real existing variety + its own type, so would_mint is False.
    from stone_pipeline.stages.curate import existing as cur_existing
    v = next(iter(cur_existing.load_all_existing()["slab"].varieties))
    name, ntype = v["Name"], v["type"]                       # ntype is the normalized type slug
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"type": {ntype}})
    views.mint(name, stone_type=ntype.title())               # a stored mint decision for an EXISTING variety
    res = curate.build_curation([_scrape(name, raw_type=ntype.title())], ref)
    assert not any(p.get("kind") == "archived_type" and p.get("variant") == name
                   for p in res.pending_confirm)             # no card for the existing variety
    assert not _minted_keys(res, proj.norm(name).replace(" ", "_"))   # no duplicate mint


def test_mint_with_only_a_restricted_colour_holds(ref, monkeypatch):
    # fix 2: dropping the ONLY colour (restricted) must HOLD, never fall back to a default (no guessing).
    views.mint("Novum Only Pink", stone_type="Granite")
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"color": {proj.norm("Pink")}})
    res = curate.build_curation([_mk_row("Novum Only Pink", "Pink")], ref)
    assert _is_held(res, "Novum Only Pink")
    assert not _minted_keys(res, "novum_only_pink")


def test_mint_drops_a_restricted_colour_but_keeps_the_rest(ref, monkeypatch):
    # fix 2: a non-empty remainder after the drop mints with what remains (no default, no hold).
    views.mint("Novum Two Colour", stone_type="Granite")
    rows = [_mk_row("Novum Two Colour", "Pink", sk="a"), _mk_row("Novum Two Colour", "Grey", sk="b")]
    base = _posts(curate.build_curation(rows, ref), "novum_two_colour")
    assert base and any("Pink" in p["color"] and "Grey" in p["color"] for p in base)   # both colours flow
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"color": {proj.norm("Pink")}})
    posts = _posts(curate.build_curation(rows, ref), "novum_two_colour")
    assert posts and all("Pink" not in p["color"] and "Grey" in p["color"] for p in posts)


def test_decision_audit_suppresses_the_mint_gap_only_for_a_ve2_hold():
    # fix 6c: the "Mint not applied" gap is suppressed only when THIS (source, spelling) is held by a VE2
    # freeze card -- not by another vendor's card, and not for non-VE2 holds. One hold, one review entry.
    from stone_pipeline.stages import decision_audit
    st = SimpleNamespace(is_mint=True, name="Novum Frozen Stone", display="Novum Frozen Stone",
                         stone_type="Granite", source="varsha", origin_iso="")
    decisions = SimpleNamespace(statements={("varsha", "novum frozen stone"): st},
                                bindings={}, vendor_origins={})
    held = decision_audit.audit([], set(), set(), decisions, set(),
                                held_ve2={("varsha", "novum frozen stone")})
    assert not any(g["decision"] == "mint" for g in held)         # same (source, spelling) held -> no gap
    other = decision_audit.audit([], set(), set(), decisions, set(),
                                 held_ve2={("zucchi", "novum frozen stone")})
    assert any(g["decision"] == "mint" for g in other)            # another vendor's card never hides this gap
    assert any(g["decision"] == "mint"                            # no VE2 hold -> the gap fires
               for g in decision_audit.audit([], set(), set(), decisions, set()))


def test_a_gapped_scrape_aliasing_to_an_archived_variety_is_held(ref, monkeypatch):
    # fix 1 (round 2): the alias to an archived variety never serves, so curate surfaces an archived_variety
    # card (the alias row is still recorded). No card when the owner is active.
    v = next(iter(cur_existing.load_all_existing()["slab"].varieties))
    name, ntype = v["Name"], v["type"]
    active = curate.build_curation([_scrape(name, raw_type=ntype.title())], ref)
    assert not any(p.get("kind") == "archived_variety" for p in active.pending_confirm)   # owner active -> no card

    real = cur_existing.load_all_existing()
    for imp in real.values():
        for e in imp.varieties:
            if e["Name"] == name and e["type"] == ntype:
                e["archived"] = True
        imp.by_name_type.clear(); imp.by_name.clear()
        for e in imp.varieties:
            imp.by_name_type[(proj.norm(e["Name"]), e["type"])] = e
            imp.by_name[proj.norm(e["Name"])] = e
    monkeypatch.setattr(curate, "load_all_existing", lambda: real)
    held = curate.build_curation([_scrape(name, raw_type=ntype.title())], ref)
    card = next((p for p in held.pending_confirm if p.get("kind") == "archived_variety"), None)
    assert card is not None                                  # owner archived -> held
    assert card["stone_type"]                                # carries the owner's type (its own name|type ref)


def test_an_archived_block_key_does_not_flag_a_slab_listing(ref, monkeypatch):
    # fix 2: the archived check is the ROW'S OWN branch. An archived BLOCK Key must not flag a SLAB listing
    # (whose "mint as new" would create a duplicate) -- only the slab variety's own archived state matters.
    name = ntype = None
    real = cur_existing.load_all_existing()
    for e in real["slab"].varieties:                        # a variety present in both slab and block
        if (proj.norm(e["Name"]), e["type"]) in real["block"].by_name_type:
            name, ntype = e["Name"], e["type"]
            break
    if name is None:
        import pytest as _pytest
        _pytest.skip("no variety present in both slab and block")
    for e in real["block"].varieties:                       # flip ONLY the block Key archived
        if e["Name"] == name and e["type"] == ntype:
            e["archived"] = True
    real["block"].by_name_type[(proj.norm(name), ntype)]["archived"] = True
    monkeypatch.setattr(curate, "load_all_existing", lambda: real)
    res = curate.build_curation([_scrape(name, raw_type=ntype.title())], ref)   # a SLAB listing (default branch)
    assert not any(p.get("kind") == "archived_variety" for p in res.pending_confirm)


def test_a_global_mint_is_held_by_any_vendors_ve2_card():
    # fix 1: a GLOBAL mint statement ('' source) is held by that spelling's VE2 card from ANY vendor.
    from stone_pipeline.stages import decision_audit
    st = SimpleNamespace(is_mint=True, name="Novum Frozen Stone", display="Novum Frozen Stone",
                         stone_type="Granite", source="", origin_iso="")
    decisions = SimpleNamespace(statements={("", "novum frozen stone"): st}, bindings={}, vendor_origins={})
    held = decision_audit.audit([], set(), set(), decisions, set(),
                                held_ve2={("varsha", "novum frozen stone")})
    assert not any(g["decision"] == "mint" for g in held)   # global held by varsha's card -> no duplicate gap


def test_two_listings_of_one_name_one_typed_onto_archived_stay_two_cards():
    # fix 3: the archived_variety card's stone_type gives it a (name, type) ref, so write_confirm_file keeps it
    # separate from a type-less listing of the same name (which would otherwise swallow it into one card).
    from stone_pipeline.stages import decisions
    cards = [
        {"variant": "Novum X", "stone_type": "Marble", "kind": "archived_variety", "src": "varsha",
         "scraped": "novum x"},
        {"variant": "Novum X", "stone_type": "", "kind": "collision", "src": "zucchi", "scraped": "novum x"},
    ]
    assert decisions.write_confirm_file(cards) == 2   # name|marble vs name -> two distinct cards


def test_the_archived_value_quality_card_points_at_blokport_not_review(ref, monkeypatch):
    # fix 2: the QUALITY recovery text must not send the operator to a Review action that cannot set a quality.
    views.mint("Novum Only Quality", stone_type="Granite")
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"quality": {proj.norm("A")}})
    rows = [_mk_row("Novum Only Quality", "White")]   # quality A is the pack last-resort, now restricted
    rows[0].quality_name = "A"
    res = curate.build_curation(rows, ref)
    card = next((p for p in res.pending_confirm
                 if p.get("kind") == "archived_value" and p["variant"] == "Novum Only Quality"), None)
    assert card is not None                               # the quality dimension emptied -> the card is raised
    assert "Reactivate it in Blokport" in card["reason"] and "quality in Review" not in card["reason"]


def test_uncovered_missing_is_dormant_for_a_category_only_shortfall(tmp_path):
    # fix 4: when ONLY the category pcat is missing (not colour/quality), main writes an empty missing. The
    # post-filter classification must preserve that '' (not write 'restricted'), so the file is byte-identical.
    p = tmp_path / "attrs_no_category.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["category", "value", "sourceid", "status", "internal"])
        w.writerows(r + ("active", "false") for r in _ATTR_ROWS if r[0] != "category")   # NO category pcat rows
    bb = _backbone(tmp_path, [_post("slab_marble_carrara_1", "Carrara", ["Polished"], color=("White",))])
    exp = _export(tmp_path, [("v1", "slab_marble_carrara_1", "Carrara")])
    _combos, _stats, unc = tree_build.build_combinations(exp, p, [bb], None)
    entry = next(u for u in unc if u["Id"] == "v1")
    assert entry["missing"] == ""      # category-only shortfall -> empty, exactly as main writes it


def test_an_acked_product_that_cannot_be_rebuilt_is_counted(tmp_path, monkeypatch):
    # fix 5: an acked product whose tuple cannot be rebuilt (an id/vid missing) is counted, not dropped silently.
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger, now_iso
    key = "slab_marble_carrara_1"
    lpath = tmp_path / "dev.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(lpath))
    with Ledger.open(lpath, env=writethrough.ENV_NAME,
                     backend_id_fingerprint=writethrough.backend_fingerprint()) as lg:
        now = now_iso()
        lg.upsert("variation", {"key": key, "branch": "slab", "type": "Marble", "name": "Carrara",
                                "aliases": "[]", "image_url": "", "image_sha256": None, "image_model": None,
                                "volume": "", "medusa_id": "V", "in_full": 1, "payload_hash": "",
                                "state": "synced", "archived": 0, "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))
        lg.upsert("product", {"sku": "ZUCCHI-1", "source": "zucchi", "surrogate_key": "1",
                              "color": "NoSuchColour", "finish": "Polished", "quality": "A", "type": "Marble",
                              "category": "Slabs", "variation_key": key, "medusa_id": "M1", "state": "synced",
                              "created_at": now, "updated_at": now}, pk=("sku",))
    bb = _backbone(tmp_path, [_post(key, "Carrara", ["Polished"], color=("White",))])
    exp = _export(tmp_path, [("v1", key, "Carrara")])
    attrs = _attrs_status(tmp_path, archived=[("color", "White")])   # something restricted -> acked path runs
    _combos, stats, _unc = tree_build.build_combinations(exp, attrs, [bb], None)
    assert stats["acked_tuples_unrebuilt"] >= 1   # NoSuchColour has no id -> counted, not dropped silently


def test_render_dump_attributes_writes_status_and_internal(tmp_path):
    # fix 6: render._dump_attributes carries status/internal so ledger.verify matches once restricted. A revert
    # to the 3-column shape fails this.
    from stone_pipeline.ledger import render
    from stone_pipeline.ledger.db import Ledger, now_iso
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        now = now_iso()
        lg.upsert("attribute", {"category": "color", "value": "Beige", "medusa_id": "c1", "state": "synced",
                                "created_at": now, "updated_at": now}, pk=("category", "value"))
        out = tmp_path / "attrs.csv"
        render._dump_attributes(lg, out)
    with out.open(encoding="utf-8-sig", newline="") as h:
        rows = list(csv.DictReader(h))
    assert "status" in rows[0] and "internal" in rows[0]
    assert rows[0]["status"] == "active" and rows[0]["internal"] == "false"


# --- review follow-up pins (PR7): the behaviours PR5 shipped, now covered directly -------------------------

def test_the_curate_index_reads_the_archived_flag_from_the_export(tmp_path, monkeypatch):
    # LOW (PR5): load_all_existing must populate each variety's `archived` from the export STATUS column, not
    # only when a test sets it on the index by hand (existing.py line "archived": r.archived).
    p = tmp_path / "variants_export.csv"
    with p.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)", "status"])
        w.writerow(["v1", "slab_marble_carrara_1", "Carrara", "", "", "", "archived"])
        w.writerow(["v2", "slab_marble_statuario_1", "Statuario", "", "", "", "active"])
    monkeypatch.setattr(cur_existing, "existing_varieties_file", lambda: p)
    slab = cur_existing.load_all_existing()["slab"]
    assert slab.by_name_type[(proj.norm("Carrara"), "marble")]["archived"] is True
    assert slab.by_name_type[(proj.norm("Statuario"), "marble")]["archived"] is False


def test_the_archived_value_card_leaves_the_colour_field_empty(ref, monkeypatch):
    # LOW (PR5): prefilling the restricted colour on an archived_value card would just hold again, so the
    # card's colour field is left empty (minting.py raises the card with no color=).
    views.mint("Novum Empty Colour", stone_type="Granite")
    monkeypatch.setattr(minting, "restricted_values", lambda *a, **k: {"color": {proj.norm("Pink")}})
    res = curate.build_curation([_mk_row("Novum Empty Colour", "Pink")], ref)
    card = next((p for p in res.pending_confirm
                 if p.get("kind") == "archived_value" and p["variant"] == "Novum Empty Colour"), None)
    assert card is not None and card["color"] == ""


def test_render_combinations_passes_its_own_ledger_to_the_acked_rebuild(tmp_path, monkeypatch):
    # LOW (PR5): render_combinations must hand build_combinations the ledger it was given (acked_ledger=ledger),
    # so the render rebuilds its acked tuples from THAT ledger, not the default on-disk one.
    from stone_pipeline.ledger import render
    from stone_pipeline.ledger.db import Ledger
    captured = {}

    def _fake(export_p, attrs_p, backbones, products=None, assigned=None, exclude=None, *, acked_ledger=None):
        captured["ledger"] = acked_ledger
        return [], {}, []

    monkeypatch.setattr(tree_build, "build_combinations", _fake)
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        render.render_combinations(lg, tmp_path / "combos.csv")
    assert captured["ledger"] is lg
