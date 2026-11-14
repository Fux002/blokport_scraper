"""Curation loop: emit import-format additions (new variants) and learned-alias proposals
for every category, plus synonym-first attribute curation. Files are never
rewritten; deterministic uuid5 keys match the existing format.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind, ReviewFlag, TreeGap
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.matching import projections as proj
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate
from stone_pipeline.tests import _decision_views as views
from stone_pipeline.tests._existing_index import existing_index


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def test_key_is_deterministic_and_matches_format():
    k1 = new_key("Marble", "Foo Bar")
    assert k1 == new_key("Marble", "Foo Bar")  # deterministic (uuid5), stable across runs
    assert k1.startswith("marble__foo_bar_")
    # structurally a uuid suffix (5 hyphen-separated groups)
    assert len(k1.rsplit("_", 1)[1].split("-")) == 5


def test_a_guessed_bind_proposes_its_spelling_on_the_matched_key(ref):
    # a confirmed non-exact match proposes the scraped spelling as a learned alias (a proposal, never an
    # import row). A real match carries the matched variety's stable Key ('Alpine' exists as BOTH Granite and
    # Marble, so the Key -- its type is the authority -- is what disambiguates which one the spelling attaches to).
    # Use a spelling not already on the live variant, so the test asserts the ADD path, not an idempotent no-op.
    alpine_granite = curate.load_existing().by_name_type[("alpine", "granite")]
    row = CanonicalRow(src_site="polonine", surrogate_key="1",
                       variety_match_key="Alpine Qx9", variation_id="x",
                       variation_key=alpine_granite["Key"], variation_name="Alpine",
                       variation_method="fuzzy")
    result = curate.build_curation([row], ref)
    # the spelling attaches to the Granite Alpine (matched Key), never the same-named Marble one
    assert result.learned_proposals == [{"identity_key": alpine_granite["Key"], "value": "Alpine Qx9",
                                         "method": "curate_confirmed", "source": "polonine", "score": None}]


def test_exact_match_is_not_proposed_as_alias(ref):
    row = CanonicalRow(src_site="polonine", surrogate_key="2",
                       variety_match_key="Arabescato", variation_id="x",
                       variation_name="Arabescato", variation_method="exact")
    result = curate.build_curation([row], ref)
    assert result.learned_proposals == []


def _novel_row(fmt: str = "slab"):
    r = CanonicalRow(src_site="polonine", surrogate_key="3", variety_match_key="Totally Novel Xyz", raw_type="Granite",
                     format_value=fmt)
    r.add_gap(TreeGap(src_site="polonine", surrogate_key="3", raw_name="Totally Novel Xyz",
                      gap_kind=GapKind.missing_variation, nearest_existing="Something", nearest_score=40.0))
    return r


def test_new_variant_emitted_once_with_an_image_for_its_products_category(ref, monkeypatch):
    from stone_pipeline.stages import decisions

    # 'every new variety is reviewed': a genuinely-new TYPED variety HOLDS for review, never auto-minted.
    monkeypatch.setattr(decisions, "load_decisions", Decisions.empty)
    held = curate.build_curation([_novel_row()], ref)
    assert any(p["variant"] == "Totally Novel Xyz" for p in held.pending_confirm), "new variety must hold"
    assert not held.new_variants, "must not auto-mint before confirmation"

    # once the operator confirms it, the NEXT produce mints it: ONE variety row and ONE backbone post, whatever
    # its categories, and an image item for the category its product sits in (a slab) only.
    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "totally novel xyz"): {"action": "mint"}}))
    result = curate.build_curation([_novel_row()], ref)
    [out] = [r for r in result.new_variants if r["Name"] == "Totally Novel Xyz"]
    assert out["Key"] == new_key("Granite", "Totally Novel Xyz") and out["Type"] == "Granite"
    assert "Image" not in out and "Volume per kg (m³/kg)" not in out
    [post] = [p for p in result.backbone_new if p["variant"] == "Totally Novel Xyz"]
    assert post["key"] == out["Key"]  # backbone joins to export by this Key
    assert post["categories"] == ["slab"]
    assert [(i["key"], i["category"]) for i in result.images_to_generate] == [(out["Key"], "slab")]


def test_an_inactive_category_gets_no_image(ref, monkeypatch):
    import dataclasses
    from stone_pipeline.config import categories
    from stone_pipeline.stages import decisions
    # clearing the Medusa category id deactivates the category, no code change: a tile product's new variety
    # still mints its one row, but no icon is made for the inactive tile category
    tile = dataclasses.replace(categories._BY_NAME["tile"], pcat_id="")
    monkeypatch.setitem(categories._BY_NAME, "tile", tile)
    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "totally novel xyz"): {"action": "mint"}}))
    result = curate.build_curation([_novel_row("tile")], ref)
    assert [r["Name"] for r in result.new_variants] == ["Totally Novel Xyz"]
    assert result.backbone_new[0]["categories"] == [] and result.images_to_generate == []


def test_borderline_gap_becomes_alias_candidate_not_new_variant(ref, monkeypatch):
    # FALLBACK path (alias model off): a gap whose nearest existing variety scores high is
    # proposed as an ALIAS of that variety, not a new variant. (The tier-7 model path is covered
    # by test_alias_resolver; the confirm-ledger read-back by test_decisions.)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    nearest = next(iter(ref.variants["slab"].by_id.values())).name
    row = CanonicalRow(src_site="polonine", surrogate_key="ac1",
                       variety_match_key="Suppliers Rebrand Xyz", raw_type="Marble")
    row.add_gap(TreeGap(src_site="polonine", surrogate_key="ac1", raw_name="Suppliers Rebrand Xyz",
                        gap_kind=GapKind.missing_variation, nearest_existing=nearest, nearest_score=82.0))
    result = curate.build_curation([row], ref)
    # not proposed as a new variant
    assert all(r["Name"] != "Suppliers Rebrand Xyz" for r in result.new_variants)
    # proposed as a learned alias of the nearest existing variety, for review
    nearest_key = curate.load_existing().by_name[proj.norm(nearest)]["Key"]
    assert {"identity_key": nearest_key, "value": "Suppliers Rebrand Xyz", "method": "curate_review",
            "source": "polonine", "score": 82.0} in result.learned_proposals   # the gap's nearest score


def test_new_typed_variety_holds_unconfirmed_mints_on_yes_rejects_on_no(ref, monkeypatch):
    # The 'all new reviewed' gate on the PHASE-5 mint tail: a genuinely-new TYPED variety is a review
    # decision, never a silent auto-mint. blank -> hold; 'yes' -> mint; 'no' -> reject (stays gone).
    from stone_pipeline.stages import decisions

    def _row():
        r = CanonicalRow(src_site="polonine", surrogate_key="g9",
                         variety_match_key="Nebula Quartz Prime", raw_type="Quartzite")
        r.add_gap(TreeGap(src_site="polonine", surrogate_key="g9", raw_name="Nebula Quartz Prime",
                          gap_kind=GapKind.missing_variation, nearest_score=20.0))
        return r

    monkeypatch.setattr(decisions, "load_decisions", Decisions.empty)
    held = curate.build_curation([_row()], ref)
    p = next(p for p in held.pending_confirm if p["variant"] == "Nebula Quartz Prime")
    assert p["stone_type"] == "Quartzite" and "New variety" in p["reason"]
    assert not held.new_variants

    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "nebula quartz prime"): {"action": "mint"}}))
    minted = curate.build_curation([_row()], ref)
    assert any(r["Name"] == "Nebula Quartz Prime" for r in minted.new_variants)
    assert not any(p["variant"] == "Nebula Quartz Prime" for p in minted.pending_confirm)

    # a reject is ONE statement: it answers 'no' and it is the never-propose-again memory
    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "nebula quartz prime"): {"action": "reject"}}))
    rejected = curate.build_curation([_row()], ref)
    assert not rejected.new_variants, "a 'no' must not mint"
    assert not any(p["variant"] == "Nebula Quartz Prime" for p in rejected.pending_confirm), "a 'no' must not re-surface"


def test_review_card_image_falls_back_to_a_sibling_row_of_the_same_variety(ref, monkeypatch):
    # A review card shows the triggering row's photo; when that row has none but a SIBLING row of the same
    # variety does, the card must fall back to the sibling's image (not go blank). A variety with no photo
    # anywhere stays empty. Cosmetic evidence only -- must never affect the mint/hold decision.
    from stone_pipeline.stages import decisions
    monkeypatch.setattr(decisions, "load_decisions", Decisions.empty)

    def _gap(sk, name, imgs):
        r = CanonicalRow(src_site="varsha", surrogate_key=sk, variety_match_key=name, raw_type="Quartzite",
                         raw_image_urls=imgs)
        r.add_gap(TreeGap(src_site="varsha", surrogate_key=sk, raw_name=name,
                          gap_kind=GapKind.missing_variation, nearest_score=15.0))
        return r

    # triggering row (no photo) + a sibling of the SAME new variety WITH a photo
    photo = "https://cdn.example/zephyr.jpg"
    res = curate.build_curation([_gap("z1", "Zephyr Prime Xyz", []),
                                 _gap("z2", "Zephyr Prime Xyz", [photo])], ref)
    p = next(p for p in res.pending_confirm if p["variant"] == "Zephyr Prime Xyz")
    assert p["image"] == photo, "review card must fall back to a sibling row's image"

    # a variety with no photo on ANY row stays blank (no invented image)
    res2 = curate.build_curation([_gap("n1", "Noimg Prime Xyz", [])], ref)
    p2 = next(p for p in res2.pending_confirm if p["variant"] == "Noimg Prime Xyz")
    assert p2["image"] == "", "no photo anywhere -> card stays empty, never fabricated"


def test_new_variant_emits_one_backbone_entry(ref, monkeypatch):
    from stone_pipeline.stages import decisions

    def _row():
        r = CanonicalRow(src_site="polonine", surrogate_key="bb1",
                         variety_match_key="Brand New Stone", raw_type="Granite",
                         color_name="Blue", quality_name="A", finish_name="Polished")
        r.add_gap(TreeGap(src_site="polonine", surrogate_key="bb1", raw_name="Brand New Stone",
                          gap_kind=GapKind.missing_variation, nearest_score=30.0))
        return r

    # once confirmed it mints ONE backbone entry for the variety
    monkeypatch.setattr(decisions, "load_decisions",
                        lambda: Decisions.from_legacy({("", "brand new stone"): {"action": "mint"}}))
    result = curate.build_curation([_row()], ref)
    [post] = [p for p in result.backbone_new if p["variant"] == "Brand New Stone"]
    assert "category" not in post
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
    assert not any(r["Name"] == "Z Astoria" for r in result.new_variants)
    assert any(s["raw_name"] == "Z Astoria" for s in result.suspicious_names)
    assert result.counts["suspicious_names"] >= 1




def test_curation_does_not_modify_reference_files(ref, tmp_path):
    # build_curation only READS the existing-variety file (the live export, else the committed base); it
    # must not write to catalog_source/from_medusa
    import os
    from stone_pipeline.reference.loaders import existing_varieties_file
    ref_path = str(existing_varieties_file())
    before = {ref_path: os.path.getmtime(ref_path)}
    curate.build_curation([], ref)
    after = {p: os.path.getmtime(p) for p in before}
    assert before == after


def test_typeless_variety_is_held_then_mints_with_a_seed_type(ref):
    # a variety with no resolvable stone type is HELD (never shipped type-less); the operator assigns a
    # type via the review (seed_type), and the next produce mints it with that type + a typed Key.

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
    assert not any(r["Name"] == "Karur Special White" for r in res.new_variants)

    views.mint("Karur Special White", stone_type="Granite")
    res2 = curate.build_curation([typeless()], ref)
    posts = [p for p in res2.backbone_new if p["variant"] == "Karur Special White"]
    assert posts and all(p["stone_type"] == "Granite" for p in posts)   # minted with the assigned type
    assert all(p["key"].startswith("granite__") for p in posts)          # and a typed Key slug
    assert not any(p["variant"] == "Karur Special White" for p in res2.pending_confirm)


def _inject_existing(monkeypatch, *entries):
    """Populate curate's existing-variety index with (name, type) varieties. The index normally comes from
    variants_export (Id-bearing, ABSENT locally), so without this the multi-type BUG6 path never executes and a
    test would pass through an unrelated path (false confidence)."""
    monkeypatch.setattr(curate, "load_existing",
                        lambda: existing_index(*((new_key(typ, name), name, "") for name, typ in entries)))


def _ocean_blue(raw_type):
    r = CanonicalRow(src_site="varsha", surrogate_key="ob", variety_match_key="Ocean Blue", raw_type=raw_type)
    r.add_gap(TreeGap(src_site="varsha", surrogate_key="ob", raw_name="Ocean Blue",
                      gap_kind=GapKind.missing_variation, nearest_existing="Ocean Blue", nearest_score=100.0))
    return r


def test_new_type_on_existing_multitype_name_holds_not_mint(ref, monkeypatch):
    # BUG6 / HOLD-never-guess: 'Ocean Blue' exists as Granite + Marble but NOT Onyx. A scrape typed 'Onyx' (a
    # NEW type for the name) must HOLD for the operator to confirm, never silently mint 'Ocean Blue Onyx'.
    _inject_existing(monkeypatch, ("Ocean Blue", "Granite"), ("Ocean Blue", "Marble"))
    res = curate.build_curation([_ocean_blue("Onyx")], ref)
    assert any(p["variant"] == "Ocean Blue" for p in res.pending_confirm)         # held for confirmation
    assert not any("onyx" in row["Key"] for row in res.new_variants
                   if row["Name"] == "Ocean Blue")                                 # NOT silently minted


def test_confirmed_new_type_mints_not_absorbed_into_a_same_name_sibling(ref, monkeypatch):
    # The absorption bug behind "I can only mint it as a preset type": a CONFIRMED new type on an existing
    # multi-type name fell through to the fuzzy aliaser, which (nearest = the same name under a different
    # type) aliased it onto that sibling instead of minting ('Jasper Green Dolomite' -> 'Jasper Green
    # Marble'). With the operator's mint decision it must MINT the new type instead.
    _inject_existing(monkeypatch, ("Ocean Blue", "Granite"), ("Ocean Blue", "Marble"))
    views.mint("Ocean Blue")
    res = curate.build_curation([_ocean_blue("Onyx")], ref)
    minted = [rw["Key"] for rw in res.new_variants if rw["Name"] == "Ocean Blue"]
    assert minted == [new_key("Onyx", "Ocean Blue")], \
        "a confirmed new type must MINT, not be absorbed into a same-name sibling"


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
    minted = [row["Key"] for row in res.new_variants]
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


def test_new_type_card_names_the_owner_when_the_match_is_an_alias(ref, monkeypatch):
    # on prod, Zucchi lists 'Crystal Absolut' typed Crystal. No variety of that name exists; the name
    # is an ALIAS on the Onyx 'Absolut'. The card said "'Crystal Absolut' already exists as Onyx", which the
    # operator could not find anywhere. The card must name the owner and say it is an alias.
    monkeypatch.setattr(curate, "load_existing",
                        lambda: existing_index(("onyx__absolut_x", "Absolut", "Crystal Absolut")))
    r = CanonicalRow(src_site="zucchi", surrogate_key="ca", variety_match_key="Crystal Absolut", raw_type="Crystal")
    r.add_gap(TreeGap(src_site="zucchi", surrogate_key="ca", raw_name="Crystal Absolut",
                      gap_kind=GapKind.missing_variation, nearest_existing="Absolut", nearest_score=60.0))
    res = curate.build_curation([r], ref)
    card = next(p for p in res.pending_confirm if p["variant"] == "Crystal Absolut")
    assert card["kind"] == "new_type"
    assert "alias of 'Absolut' (Onyx)" in card["reason"], card["reason"]
    assert "'Crystal Absolut' already exists" not in card["reason"], card["reason"]
    assert card["nearest_existing"] == "Absolut (Onyx)", card["nearest_existing"]
