"""Curation loop: emit import-format additions (forgotten aliases + new variants)
for every category, plus synonym-first attribute curation. Files are never
rewritten; deterministic uuid5 keys match the existing format.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind, ReviewFlag, TreeGap
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def test_key_is_deterministic_and_matches_format():
    k1 = curate.gen_key("slab", "Marble", "Foo Bar")
    k2 = curate.gen_key("slab", "Marble", "Foo Bar")
    assert k1 == k2  # deterministic (uuid5), stable across runs
    assert k1.startswith("slab_marble_foo_bar_")
    # structurally a uuid suffix (5 hyphen-separated groups)
    assert len(k1.rsplit("_", 1)[1].split("-")) == 5
    # block variant of the same material gets a different prefix, same name slug
    assert curate.gen_key("block", "Marble", "Foo Bar").startswith("block_marble_foo_bar_")


def test_alias_addition_preserves_and_augments(ref):
    # a confirmed non-exact match proposes adding the scraped spelling as an alias. A real match carries the
    # matched variety's stable Key ('Alpine' exists as BOTH Granite and Marble, so the Key -- its type is
    # the authority -- is what disambiguates which one the spelling attaches to). Use a spelling not already
    # on the live variant, so the test asserts the ADD path, not an idempotent no-op.
    alpine_granite = curate.load_existing("slab").by_name_type[("alpine", "granite")]
    row = CanonicalRow(src_site="polonine", surrogate_key="1",
                       variety_match_key="Alpine Qx9", variation_id="x",
                       variation_key=alpine_granite["Key"], variation_name="Alpine",
                       variation_method="fuzzy")
    result = curate.build_curation([row], ref)
    slab = result.alias_additions["slab"]
    assert slab, "expected an alias addition for Alpine"
    entry = next(e for e in slab if e["Key"] == alpine_granite["Key"])
    assert "Alpine Qx9" in entry["_added"]
    assert entry["_status"] == "confirmed"
    # existing aliases are preserved (not overwritten)
    assert "|" in entry["Aliases"] and len(entry["Aliases"]) > len("ALPINE")
    # the spelling attaches to the Granite Alpine (matched Key), never the same-named Marble one
    assert entry["Key"] == alpine_granite["Key"]
    # Image is re-linked to the clean deterministic {Key}.png (blank if the variant has none),
    # never the export's internal/blank value
    expect = curate.image_url(curate.image_filename(alpine_granite["Key"])) if (alpine_granite.get("Image") or "").strip() else ""
    assert entry["Image"] == expect


def test_exact_match_is_not_proposed_as_alias(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="2",
                       variety_match_key="Arabescato", variation_id="x",
                       variation_name="Arabescato", variation_method="exact")
    result = curate.build_curation([row], ref)
    assert all(e["Name"] != "Arabescato" for e in result.alias_additions["slab"])


def test_new_variant_emitted_for_active_categories(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="3",
                       variety_match_key="Totally Novel Xyz", raw_type="Granite")
    row.add_gap(TreeGap(src_site="polonine", surrogate_key="3", raw_name="Totally Novel Xyz",
                        gap_kind=GapKind.missing_variation, nearest_existing="Something", nearest_score=40.0))
    result = curate.build_curation([row], ref)
    # variant created in EVERY active category (uniform catalog): slab, block, tile.
    keys = {}
    for branch in ("slab", "block", "tile"):
        rows = [r for r in result.new_variants[branch] if r["Name"] == "Totally Novel Xyz"]
        assert len(rows) == 1, f"expected new variant in {branch}"
        out = rows[0]
        keys[branch] = out["Key"]
        # only the product-backed branch (slab) carries an image link; block/tile fan-out blank
        assert out["Image"].endswith(f"{out['Key']}.png") if branch == "slab" else out["Image"] == ""
        assert "Volume per kg (m³/kg)" in out
        post = next(p for p in result.backbone_new[branch] if p["variant"] == "Totally Novel Xyz")
        assert post["image_file"] == f"{out['Key']}.png"
        assert post["key"] == out["Key"]  # backbone joins to export by this Key
    # the product was observed as a SLAB, so only slab is product-backed and gets an
    # image; the block/tile fan-out copies have no product -> no image generated (no cost).
    posts = {b: next(p for p in result.backbone_new[b] if p["variant"] == "Totally Novel Xyz")
             for b in ("slab", "block", "tile")}
    assert posts["slab"]["product_backed"] is True
    assert posts["block"]["product_backed"] is False
    assert posts["tile"]["product_backed"] is False
    gen = {i["image_filename"] for i in result.images_to_generate}
    assert f"{keys['slab']}.png" in gen           # product-backed -> image
    assert f"{keys['block']}.png" not in gen       # fan-out, no product -> no image
    assert f"{keys['tile']}.png" not in gen        # fan-out, no product -> no image


def test_active_branches_gates_tiles_on_config(monkeypatch):
    import dataclasses
    from stone_pipeline.config import settings
    assert curate.active_branches() == ("slab", "block", "tile")  # tiles active (dev pcat set)
    # clearing the Medusa category id deactivates the category, no code change
    tile = dataclasses.replace(settings._BY_NAME["tile"], pcat_id="")
    monkeypatch.setitem(settings._BY_NAME, "tile", tile)
    assert curate.active_branches() == ("slab", "block")


def test_borderline_gap_becomes_alias_candidate_not_new_variant(ref, monkeypatch):
    # FALLBACK path (alias model off): a gap whose nearest existing variety scores high is
    # proposed as an ALIAS of that variety, not a new variant. (The tier-7 model path is covered
    # by test_alias_resolver; the confirm-ledger read-back by test_decisions.)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    nearest = next(iter(ref.variants_slabs.by_id.values())).name
    row = CanonicalRow(src_site="polonine", surrogate_key="ac1",
                       variety_match_key="Suppliers Rebrand Xyz", raw_type="Marble")
    row.add_gap(TreeGap(src_site="polonine", surrogate_key="ac1", raw_name="Suppliers Rebrand Xyz",
                        gap_kind=GapKind.missing_variation, nearest_existing=nearest, nearest_score=82.0))
    result = curate.build_curation([row], ref)
    # not proposed as a new variant
    assert all(r["Name"] != "Suppliers Rebrand Xyz" for r in result.new_variants["slab"])
    # proposed as an alias of the nearest existing variety, needs_review
    target = next(r for r in result.alias_additions["slab"] if r["Name"] == nearest)
    assert "Suppliers Rebrand Xyz" in target["_added"]
    assert target["_status"] == "needs_review"


def test_new_variant_emits_backbone_entry_per_active_category(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="bb1",
                       variety_match_key="Brand New Stone", raw_type="Granite",
                       color_name="Blue", quality_name="A", finish_name="Polished")
    row.add_gap(TreeGap(src_site="polonine", surrogate_key="bb1", raw_name="Brand New Stone",
                        gap_kind=GapKind.missing_variation, nearest_score=30.0))
    result = curate.build_curation([row], ref)
    for branch, cat in (("slab", "Slabs"), ("block", "Blocks"), ("tile", "Tiles")):
        post = next(p for p in result.backbone_new[branch] if p["variant"] == "Brand New Stone")
        assert post["category"] == cat
        assert post["stone_type"] == "Granite"
        assert post["color"] == ["Blue"]
        assert "Polished" in post["finishes"]
        assert post["qualities"] == ["A"]
        assert post["key"]  # the deterministic join key; Medusa assigns the Id on import


def test_backbone_update_flags_missing_value_with_verdict(ref):
    variety = ref.backbone.lookup("Verde Ubatuba")
    bad_color = "Pink" if "Pink" not in variety.colors else "Orange"
    row = CanonicalRow(src_site="polonine", surrogate_key="bu1",
                       variation_id="x", variation_name="Verde Ubatuba", variation_method="exact",
                       variation_confidence="high", type_name=variety.stone_type,
                       color_name=bad_color, finish_name=variety.finishes[0],
                       quality_name=variety.qualities[0])
    from stone_pipeline.stages import reconcile_tree
    reconcile_tree.reconcile_row(row, ref, reconcile_tree.ReconcileStats())
    result = curate.build_curation([row], ref)
    upd = next(u for u in result.backbone_updates if u["variety"] == "Verde Ubatuba")
    assert upd["attribute"] == "color"
    assert upd["add_value"] == bad_color
    assert upd["verdict"] == "likely_real"  # exact match -> trust the missing-value signal


def test_backbone_update_flags_missing_finish(ref):
    # SAME leaf-growth path as colour, for the FINISH vocab: an existing variety sold in a finish not yet in
    # its allowed set surfaces a finish leaf-addition (not a new variety, not a duplicate).
    variety = ref.backbone.lookup("Verde Ubatuba")
    finish_vocab = [c for c, _ in ref.attributes.by_category.get("finish", {}).values()]
    bad_finish = next(f for f in finish_vocab if f not in variety.finishes)
    row = CanonicalRow(src_site="polonine", surrogate_key="fu1",
                       variation_id="x", variation_name="Verde Ubatuba", variation_method="exact",
                       variation_confidence="high", type_name=variety.stone_type,
                       color_name=variety.colors[0], finish_name=bad_finish,
                       quality_name=variety.qualities[0])
    from stone_pipeline.stages import reconcile_tree
    reconcile_tree.reconcile_row(row, ref, reconcile_tree.ReconcileStats())
    result = curate.build_curation([row], ref)
    upd = next(u for u in result.backbone_updates
               if u["variety"] == "Verde Ubatuba" and u["attribute"] == "finish")
    assert upd["add_value"] == bad_finish
    assert upd["verdict"] == "likely_real"


def test_backbone_update_flags_missing_quality(ref):
    # SAME leaf-growth path for the QUALITY vocab.
    variety = ref.backbone.lookup("Verde Ubatuba")
    qual_vocab = [c for c, _ in ref.attributes.by_category.get("quality", {}).values()]
    bad_quality = next(q for q in qual_vocab if q not in variety.qualities)
    row = CanonicalRow(src_site="polonine", surrogate_key="qu1",
                       variation_id="x", variation_name="Verde Ubatuba", variation_method="exact",
                       variation_confidence="high", type_name=variety.stone_type,
                       color_name=variety.colors[0], finish_name=variety.finishes[0],
                       quality_name=bad_quality)
    from stone_pipeline.stages import reconcile_tree
    reconcile_tree.reconcile_row(row, ref, reconcile_tree.ReconcileStats())
    result = curate.build_curation([row], ref)
    upd = next(u for u in result.backbone_updates
               if u["variety"] == "Verde Ubatuba" and u["attribute"] == "quality")
    assert upd["add_value"] == bad_quality
    assert upd["verdict"] == "likely_real"


def test_attribute_curation_suggests_synonym(ref):
    row = CanonicalRow(src_site="zucchi", surrogate_key="4")
    row.add_flag(ReviewFlag(field="type", code=FlagCode.attr_unresolved, raw_value="Semiprecious"))
    attr = curate.build_attribute_curation([row], ref)
    entry = next(a for a in attr if a["raw_value"] == "Semiprecious")
    assert entry["suggested_value"] == "Semi-Precious Stone"
    assert entry["recommended_action"] in ("synonym", "synonym?")


def test_attribute_curation_ignores_non_attribute_vocab(ref):
    # a 'variation' unresolved is a variety (owned by the confirm queue), NOT an attribute vocab. It must
    # be skipped, never crash attribute curation, which only resolves type/colour/finish/quality.
    row = CanonicalRow(src_site="zucchi", surrogate_key="5")
    row.add_flag(ReviewFlag(field="variation", code=FlagCode.attr_unresolved, raw_value="Some New Stone"))
    row.add_flag(ReviewFlag(field="color", code=FlagCode.attr_unresolved, raw_value="Blackish"))
    attr = curate.build_attribute_curation([row], ref)
    assert not any(a["vocab"] == "variation" for a in attr)   # skipped, no crash
    assert any(a["vocab"] == "color" for a in attr)           # real attr vocab still processed


def test_looks_like_artifact_heuristic():
    f = curate._looks_like_artifact
    assert f("Z Astoria") and f("X Blue") and f("") and f("123") and f("A")     # codes / junk
    assert f("Super – 1.08") and f("Marjan No. 426") and f("883 Black") and f("Matrix 3D")  # number codes
    assert not (f("G682") or f("G032") or f("G684 (Fuding Black)"))              # granite G-codes kept
    # ambiguous 2-char codes ('Zb') are the corpus cleaner's job, not this single-name heuristic
    assert not f("Zb Patagonia")
    assert not (f("Carrara") or f("Mona Lisa") or f("Verde Star") or f("Blue Pearl"))
    assert not (f("La Perla") or f("El Dorado") or f("Mt Blanc"))               # real 2-char leads kept
    assert not (f("G682") or f("G032") or f("G682 (Sunset Gold)"))              # granite G-codes kept
    # L#10: a purely non-Latin name folds to an empty match_key (two would mint the SAME Key) -> artifact
    assert f("大理石") and f("Мрамор")                                            # CJK / Cyrillic -> rejected
    assert not f("São Gabriel")                                                  # accented Latin still folds fine


def test_code_like_name_flagged_not_minted(ref):
    # a leftover supplier code (single-letter prefix) must NOT become a variant + image;
    # it is routed to review/suspicious_variety_names.csv instead
    row = CanonicalRow(src_site="varsha", surrogate_key="z1",
                       variety_match_key="Z Astoria", raw_type="Granite")
    row.add_gap(TreeGap(src_site="varsha", surrogate_key="z1", raw_name="Z Astoria",
                        gap_kind=GapKind.missing_variation, nearest_existing="Something", nearest_score=40.0))
    result = curate.build_curation([row], ref)
    assert not any(r["Name"] == "Z Astoria" for b in result.new_variants.values() for r in b)
    assert any(s["raw_name"] == "Z Astoria" for s in result.suspicious_names)
    assert result.counts["suspicious_names"] >= 1


def test_alias_decision_routes_spelling_onto_target_and_mints_nothing(ref):
    # 'White G' is a lone-letter code whose base ('White') is generic, so with no decision it HOLDS for
    # review (lands in pending_confirm). An operator 'alias -> Alpine' decision must instead route the
    # spelling onto Alpine (an existing variety) as a confirmed alias, mint nothing, and clear the hold.
    from stone_pipeline.config import decisions_store
    from stone_pipeline.stages import decisions

    def curate_white_g():
        row = CanonicalRow(src_site="varsha", surrogate_key="wg1", variety_match_key="White G",
                           raw_type="Granite")
        row.add_gap(TreeGap(src_site="varsha", surrogate_key="wg1", raw_name="White G",
                            gap_kind=GapKind.missing_variation, nearest_existing="Something",
                            nearest_score=40.0))
        return curate.build_curation([row], ref)

    # baseline: no decision -> held for review, not minted, not aliased
    base = curate_white_g()
    assert "White G" in [p["variant"] for p in base.pending_confirm]
    assert not any(r["Name"] == "White G" for b in base.new_variants.values() for r in b)
    # the held row states WHY in a dedicated 'reason' (built by the one _code_reason helper, naming the
    # likely base variety to alias to) and never puts the raw code token in the colour field
    held = next(p for p in base.pending_confirm if p["variant"] == "White G")
    assert held["reason"] == curate._code_reason("lone_letter", "White") and held["color"] == ""

    # decide: alias 'White G' onto the existing variety 'Alpine'
    decisions_store.set_variety_decision("White G", "alias", alias_of="Alpine")
    assert decisions.load_alias_decisions() == {"white g": "Alpine"}

    result = curate_white_g()
    # no longer held, never minted
    assert "White G" not in [p["variant"] for p in result.pending_confirm]
    assert not any(r["Name"] == "White G" for b in result.new_variants.values() for r in b)
    # routed onto Alpine as a confirmed alias (so Alpine's payload gains the spelling -> re-serves)
    entry = next(e for e in result.alias_additions["slab"] if e["Name"] == "Alpine")
    assert "White G" in entry["_added"] and entry["_status"] == "confirmed"


def test_curation_does_not_modify_reference_files(ref, tmp_path):
    # build_curation only READS the immutable export; it must not write to catalog_source/from_medusa
    import os
    ref_path = str(curate.SETTINGS.paths.export_file)
    before = {ref_path: os.path.getmtime(ref_path)} if os.path.exists(ref_path) else {}
    curate.build_curation([], ref)
    after = {p: os.path.getmtime(p) for p in before}
    assert before == after


def test_typeless_variety_is_held_then_mints_with_a_seed_type(ref):
    # a variety with no resolvable stone type is HELD (never shipped type-less); the operator assigns a
    # type via the review (seed_type), and the next produce mints it with that type + a typed Key.
    from stone_pipeline.config import decisions_store

    def typeless():
        r = CanonicalRow(src_site="varsha", surrogate_key="n1", variety_match_key="Karur Special White",
                         raw_type="")
        r.add_gap(TreeGap(src_site="varsha", surrogate_key="n1", raw_name="Karur Special White",
                          gap_kind=GapKind.missing_variation, nearest_existing="Something", nearest_score=30.0))
        return r

    res = curate.build_curation([typeless()], ref)
    held = next(p for p in res.pending_confirm if p["variant"] == "Karur Special White")
    assert held["reason"] == ("No stone type detected. Assign the correct type to mint it. "
                              "A variety cannot exist without a type.")
    assert not any(r["Name"] == "Karur Special White" for b in res.new_variants.values() for r in b)

    decisions_store.set_variety_decision("Karur Special White", "mint", seed_type="Granite")
    res2 = curate.build_curation([typeless()], ref)
    posts = [p for b in ("slab", "block", "tile")
             for p in res2.backbone_new[b] if p["variant"] == "Karur Special White"]
    assert posts and all(p["stone_type"] == "Granite" for p in posts)   # minted with the assigned type
    assert all("granite" in p["key"] for p in posts)                    # and a typed Key slug
    assert not any(p["variant"] == "Karur Special White" for p in res2.pending_confirm)


def test_new_type_on_existing_multitype_name_holds_not_mint(ref):
    # BUG6 / HOLD-never-guess: 'Ocean Blue' exists under granite/limestone/marble/travertine but NOT
    # quartzite. A scrape typed 'Quartzite' (a NEW type for the name) must HOLD for the operator to confirm
    # it is a genuinely new variety, never silently mint 'Ocean Blue Quartzite' (a mis-tag would phantom it).
    from stone_pipeline.config import decisions_store

    def ocean_blue_quartzite():
        r = CanonicalRow(src_site="varsha", surrogate_key="ob1", variety_match_key="Ocean Blue",
                         raw_type="Quartzite")
        r.add_gap(TreeGap(src_site="varsha", surrogate_key="ob1", raw_name="Ocean Blue",
                          gap_kind=GapKind.missing_variation, nearest_existing="Ocean Blue", nearest_score=100.0))
        return r

    res = curate.build_curation([ocean_blue_quartzite()], ref)
    assert any(p["variant"] == "Ocean Blue" for p in res.pending_confirm)   # held for confirmation
    assert not any("ocean_blue" in row["Key"] and "quartzite" in row["Key"]
                   for b in res.new_variants.values() for row in b)          # NOT silently minted

    # operator confirms it IS a genuinely new variety -> the next produce mints it as the scraped new type
    decisions_store.set_variety_decision("Ocean Blue", "mint")
    res2 = curate.build_curation([ocean_blue_quartzite()], ref)
    posts = [p for b in ("slab", "block", "tile")
             for p in res2.backbone_new[b] if p["variant"] == "Ocean Blue" and p["stone_type"] == "Quartzite"]
    assert posts, "an explicit mint decision un-holds the new-type variety"
    assert all("quartzite" in p["key"] for p in posts)                      # minted under the new typed Key


def test_non_canonical_type_holds_instead_of_minting_a_malformed_variety(ref):
    # A raw supplier type that no synonym maps to a canonical Medusa type (e.g. 'Notarealtype') is NOT a
    # real type: the variety must HOLD for review (type-less), never mint under a garbage type slug -- the
    # class that produced the 'slab_semiprecious_smoky' stray. Validates the RESULT, so a canonical type
    # still passes; only an un-normalized non-canonical value is rejected.
    r = CanonicalRow(src_site="varsha", surrogate_key="n1", variety_match_key="Weird Novel Stone",
                     raw_type="Notarealtype")
    r.add_gap(TreeGap(src_site="varsha", surrogate_key="n1", raw_name="Weird Novel Stone",
                      gap_kind=GapKind.missing_variation, nearest_existing="", nearest_score=0.0))
    res = curate.build_curation([r], ref)
    assert any(p["variant"] == "Weird Novel Stone" for p in res.pending_confirm)   # held, asked for a type
    minted = [row["Key"] for b in res.new_variants.values() for row in b]
    assert not any("weird_novel_stone" in k for k in minted), f"minted under a non-canonical type: {minted}"
    assert not any("notarealtype" in k for k in minted), f"garbage type slug reached a Key: {minted}"


# -- review evidence image: durable supplier url over the churn-deleted improved/ key ---------------

def test_review_evidence_prefers_durable_supplier_url_over_improved_key():
    # The improved/ render is hard-deleted with the product on a Blokport clear/remove, so the review
    # queue would 404 until the next produce. The supplier url survives churn, so it wins when present.
    row = CanonicalRow(src_site="varsha", src_url="https://varshastones.slabware.com/Product-Details.aspx?ID=1")
    row.raw_image_urls = ["https://varshastones.slabware.com/backendGranite/.../photo.jpg"]
    row.image_keys = ["https://blokport-dev-staging-3e58a6.s3.eu-west-1.amazonaws.com/dev/products/improved/varsha/abc.jpg"]
    ev = curate._review_evidence(row)
    assert ev["image"] == row.raw_image_urls[0]          # durable supplier photo, not the ephemeral improved/ key
    assert ev["src_url"].endswith("ID=1")


def test_review_evidence_falls_back_to_improved_key_when_no_supplier_url():
    # An API row that captured no raw url still gets a thumbnail from the processed key (better than none).
    row = CanonicalRow(src_site="varsha")
    row.raw_image_urls = []
    row.image_keys = ["https://.../dev/products/improved/varsha/abc.jpg"]
    assert curate._review_evidence(row)["image"] == row.image_keys[0]
