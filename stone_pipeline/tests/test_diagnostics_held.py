"""The diagnostics 'what was skipped and why' worklist: validate.held_breakdown groups the rejected rows by
the requirement rule, carries the operator-facing title/kind/recovery from the pinned registry, and orders
actionable holds before self-healing ones. Every hard rule must carry operator text, so a new requirement
cannot ship without a worklist entry."""

from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow, RejectReason
from stone_pipeline.gates.requirements import MEDUSA_REQUIREMENTS
from stone_pipeline.stages import validate


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


def test_no_rejects_is_an_empty_worklist():
    assert validate.held_breakdown([]) == []
