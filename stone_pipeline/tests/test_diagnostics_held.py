"""The diagnostics 'what was skipped and why' worklist: validate.held_breakdown groups the rejected rows by
the requirement rule, carries the operator-facing title/kind/recovery from the pinned registry, and orders
actionable holds before self-healing ones. Every hard rule must carry operator text, so a new requirement
cannot ship without a worklist entry."""

from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow, RejectReason
from stone_pipeline.gates.requirements import MEDUSA_REQUIREMENTS, WORKLIST_REFINEMENTS
from stone_pipeline.stages import validate

# the diagnostics table's layer labels; a worklist stage must name one (or two, slash-joined) of these so
# the panel can point the operator at the layer that detected the hold
_LAYERS = {"Ingest", "Keys Dedupe", "Format", "Normalize", "Match Variation", "Reconcile", "Derive",
           "Images", "Constants", "Validate"}


def _row(name: str, *rejects: tuple[str, str]) -> CanonicalRow:
    r = CanonicalRow(src_site="zucchi", surrogate_key=name.lower().replace(" ", "-"), variation_name=name)
    for rule, detail in rejects:
        r.add_reject(RejectReason(rule=rule, detail=detail))
    return r


def test_held_breakdown_groups_by_rule_with_operator_text_and_examples():
    rejects = [
        _row("Blue Dunes", ("tree_gap", "missing_variation")),
        _row("Ocean X", ("required_id_null", "color_id")),
        _row("Ocean Y", ("required_id_null", "finish_id")),
        _row("Sahara", ("dimension_invalid", "length")),
    ]
    out = validate.held_breakdown(rejects)
    by = {g["rule"]: g for g in out}
    assert by["required_id_null"]["count"] == 2
    assert by["tree_gap"]["count"] == 1 and by["dimension_invalid"]["count"] == 1
    # operator-facing fields are populated from the registry
    assert by["required_id_null"]["title"] and by["required_id_null"]["recovery"]
    assert by["dimension_invalid"]["self_heals"] is True and by["tree_gap"]["self_heals"] is False
    # an example carries the product name and the specific detail
    ex = by["required_id_null"]["examples"][0]
    assert ex["name"] in {"Ocean X", "Ocean Y"} and ex["detail"] in {"color_id", "finish_id"}
    # each group names the layer that detected it (the "where"): size at Derive, a new variety at Match/Reconcile
    assert by["dimension_invalid"]["stage"] == "Derive"
    assert by["tree_gap"]["stage"] == "Match Variation / Reconcile"


def test_actionable_holds_sort_before_self_healing():
    out = validate.held_breakdown([
        _row("A", ("dimension_invalid", "width")),      # transient
        _row("B", ("required_id_null", "type_id")),     # decision
        _row("C", ("owner_missing", "company_id")),     # config
    ])
    assert [g["kind"] for g in out] == ["decision", "config", "transient"]


def test_a_row_with_two_rules_appears_under_each():
    # a gapped row has both a tree_gap and a null variation_id: it shows in both groups (counts are per reason)
    out = validate.held_breakdown([_row("Gap", ("tree_gap", "missing_variation"),
                                         ("required_id_null", "variation_id"))])
    assert {g["rule"] for g in out} == {"tree_gap", "required_id_null"}
    assert all(g["count"] == 1 for g in out)


def test_every_hard_rule_carries_operator_worklist_text():
    # a new requirement cannot ship without a title + recovery + a valid kind, so the panel is never blank
    for req in MEDUSA_REQUIREMENTS:
        assert req.title.strip(), f"{req.rule} has no title"
        assert req.recovery.strip(), f"{req.rule} has no recovery"
        assert req.kind in {"decision", "transient", "config"}, f"{req.rule} bad kind {req.kind!r}"


def test_every_worklist_entry_names_a_real_detecting_layer():
    # the "where" the panel shows must be one (or two, slash-joined) of the diagnostics table's own layers,
    # so a stage typo can never point at a layer that does not exist
    for entry in (*MEDUSA_REQUIREMENTS, *WORKLIST_REFINEMENTS):
        assert entry.stage.strip(), f"{entry.rule} has no detecting stage"
        parts = [p.strip() for p in entry.stage.split("/")]
        assert all(p in _LAYERS for p in parts), f"{entry.rule} stage {entry.stage!r} names a non-layer"


def test_no_rejects_is_an_empty_worklist():
    assert validate.held_breakdown([]) == []


def test_a_bound_variety_without_a_colour_is_its_own_worklist_entry():
    # prod 2026-09-17: Polonine's SPARTA WHITE binds to 'Sparta White' (Granite), a variety minted from a
    # colourless listing, so it has no colour to inherit and rejects required_id_null: color_id. The generic
    # text sent the operator to Medusa ("paste the id, or mint the variety"); nothing there fixes it. The
    # fix is the amend flow (any statement's colour documents the variety), so it is its own entry.
    bound = _row("Sparta White", ("required_id_null", "color_id"))
    bound.variation_key = "slab_granite_sparta_white_x"
    unbound = _row("Ocean X", ("required_id_null", "color_id"))     # no variety bound: the generic case
    out = validate.held_breakdown([bound, unbound])
    by = {g["rule"]: g for g in out}
    assert by["variety_no_colour"]["count"] == 1
    assert by["variety_no_colour"]["title"] == "Variety has no documented colour"
    assert "Amend the variety in Review and set a colour" in by["variety_no_colour"]["recovery"]
    assert by["variety_no_colour"]["examples"][0]["name"] == "Sparta White"
    assert by["required_id_null"]["count"] == 1 and by["required_id_null"]["examples"][0]["name"] == "Ocean X"


def test_a_listing_with_no_photo_at_all_is_its_own_entry_not_a_transient_retry():
    # prod 2026-09-17: 16 Varsha bundles carry no image url on the vendor's site. They were filed under the
    # transient "no usable photo this run ... re-lists on its own", which is true only for a photo that
    # failed to fetch or process; a listing with no source heals only when the supplier adds a photo.
    out = validate.held_breakdown([
        _row("Black Ultimate", ("no_image", "no_source")),
        _row("Alaska Gold", ("no_image", "")),
    ])
    by = {g["rule"]: g for g in out}
    assert by["supplier_no_photo"]["count"] == 1
    assert by["supplier_no_photo"]["title"] == "Supplier lists no photo for this product"
    assert by["supplier_no_photo"]["self_heals"] is True
    assert "once the supplier adds a photo" in by["supplier_no_photo"]["recovery"]
    assert by["no_image"]["count"] == 1 and by["no_image"]["examples"][0]["name"] == "Alaska Gold"


def test_validate_marks_a_no_image_reject_with_its_cause():
    # the detail is what the worklist keys on: "no_source" when the listing carries no image url at all,
    # empty when a url existed but no image came through this run (fetch or processing failed)
    from stone_pipeline.stages import validate as v
    no_source = _row("Black Ultimate"); no_source.raw_image_urls = []
    failed = _row("Alaska Gold"); failed.raw_image_urls = ["https://varsha/img/1.jpg"]
    assert v.no_image_detail(no_source) == "no_source"
    assert v.no_image_detail(failed) == ""
