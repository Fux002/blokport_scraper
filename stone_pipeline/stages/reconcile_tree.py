"""Stage 5: tree reconciliation, the shoehorn check (section 7 Stage 5, 6A).

Normalization (Stage 3) and variation (Stage 4) are resolved independently, so
nothing yet guarantees the combination is valid for the matched variety. This
stage enforces it, with the matched variety as the authority.

Validity is set membership (section 6A): the chosen colour must be in the
variety's colours, finish in its finishes, quality in its qualities. There is no
enumerated tuple table.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from stone_pipeline.core import logfmt
from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind, ReviewFlag, TreeGap
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import Backbone, BackboneVariety, ReferenceData

log = logfmt.get_logger("reconcile_tree")

# lowest-trust attribute first: snap quality, then finish; colour is not snapped
# silently because it is more identity-bearing (a wrong colour is a wrong product).
_SNAPPABLE = ("quality", "finish")


@dataclass
class ReconcileStats:
    validated: int = 0
    snapped: int = 0
    type_overridden: int = 0
    missing_leaf: int = 0
    missing_variation: int = 0
    filled_from_variety: int = 0


# attributes that may be filled from the matched variety when the scrape omits
# them. The backbone variety is the authority for these, so this is not a guess.
# Finish is deliberately excluded: a variety allows many finishes and finish is
# identity-bearing for the product, so a missing finish is not invented.
_FILLABLE_FROM_VARIETY = ("color", "quality")


def _fill_missing_from_variety(
    row: CanonicalRow, vocab: str, allowed: list[str], stats: ReconcileStats
) -> None:
    """When the scrape did not supply this attribute, take it from the matched
    variety's allowed set. A single allowed value is unambiguous (high); several
    means we use the variety's primary value and flag it for review (medium)."""
    if getattr(row, f"{vocab}_name", None):
        return  # the scrape supplied it; do not override
    if not allowed:
        return
    stats.filled_from_variety += 1
    if len(allowed) == 1:
        setattr(row, f"{vocab}_name", allowed[0])
        setattr(row, f"{vocab}_confidence", Confidence.high.name)
        setattr(row, f"{vocab}_method", "from_variety")
    else:
        setattr(row, f"{vocab}_name", allowed[0])
        setattr(row, f"{vocab}_confidence", Confidence.medium.name)
        setattr(row, f"{vocab}_method", "from_variety_primary")
        row.add_flag(ReviewFlag(field=vocab, code=FlagCode.attr_from_variety,
                                raw_value="", best_guess=allowed[0],
                                confidence=Confidence.medium, method="from_variety_primary",
                                src_url=row.src_url))


def _nearest_allowed(value: str, allowed: list[str]) -> tuple[str | None, float]:
    best, best_score = None, 0.0
    nv = proj.norm(value)
    for candidate in allowed:
        score = max(fuzz.ratio(nv, proj.norm(candidate)), fuzz.token_sort_ratio(nv, proj.norm(candidate)))
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score


def _map_id(ref: ReferenceData, vocab: str, name: str | None) -> str | None:
    if not name:
        return None
    looked = ref.attributes.resolve_id(vocab, name)
    return looked[1] if looked else None


def reconcile_row(row: CanonicalRow, ref: ReferenceData, stats: ReconcileStats) -> None:
    # A null variation cannot be validated; ensure it is in the gap queue and stop.
    if not row.variation_id:
        # rows already held distinctly in Stage 4 (no category reference yet, or a
        # standalone category) must not also raise a generic missing_variation
        _held = (GapKind.missing_tile_reference, GapKind.unsupported_category)
        if any(g.gap_kind in _held for g in row.tree_gaps):
            return
        stats.missing_variation += 1
        if not any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps):
            row.add_gap(
                TreeGap(
                    src_site=row.src_site,
                    surrogate_key=row.surrogate_key or "",
                    raw_name=row.raw_name or "",
                    normalized_name=proj.norm(row.variety_match_key or row.raw_name or ""),
                    suggested_type=row.raw_type,
                    suggested_color=row.color_name or row.raw_color,
                    suggested_finish=row.finish_name or row.raw_finish,
                    suggested_quality=row.quality_name or row.raw_quality,
                    gap_kind=GapKind.missing_variation,
                    example_src_url=row.src_url,
                )
            )
        return

    variety = ref.backbone.lookup(row.variation_name or "", stone_type=row.raw_type)
    if variety is None:
        variety = ref.backbone.lookup(row.variation_name or "")
    if variety is None:
        # variation resolved but no backbone record: keep variation, flag, do not
        # fabricate membership. Type stays as Stage 3 resolved it.
        row.add_flag(
            ReviewFlag(
                field="variation",
                code=FlagCode.attr_unresolved,
                raw_value=row.variation_name,
                method="no_backbone_record",
                confidence=Confidence.low,
                src_url=row.src_url,
            )
        )
        _finalize_ids(row, ref)
        return

    # 1. type is authoritative from the variety
    _apply_type(row, variety, ref, stats)

    # 1b. fill colour/quality from the variety when the scrape omitted them (the
    # variety is the authority; this recovers sources with no colour column)
    _fill_missing_from_variety(row, "color", variety.colors, stats)
    _fill_missing_from_variety(row, "quality", variety.qualities, stats)

    # 2. membership with snapping for the low-trust attributes
    ok = True
    if not _reconcile_attribute(row, "color", variety.colors, ref, stats, snappable=False):
        ok = False
    if not _reconcile_attribute(row, "finish", variety.finishes, ref, stats, snappable=True):
        ok = False
    if not _reconcile_attribute(row, "quality", variety.qualities, ref, stats, snappable=True):
        ok = False

    if ok:
        stats.validated += 1
    _finalize_ids(row, ref)


def _apply_type(row: CanonicalRow, variety: BackboneVariety, ref: ReferenceData, stats: ReconcileStats) -> None:
    new_type = variety.stone_type
    if row.type_name and proj.norm(row.type_name) != proj.norm(new_type):
        stats.type_overridden += 1
        row.add_flag(
            ReviewFlag(
                field="type",
                code=FlagCode.type_overridden_by_variety,
                raw_value=row.type_name,
                best_guess=new_type,
                confidence=Confidence.high,
                method="variety_authoritative",
                src_url=row.src_url,
            )
        )
    row.type_name = new_type
    row.type_id = _map_id(ref, "type", new_type)
    row.type_method = "variety_authoritative"
    row.type_confidence = Confidence.high.name


def _reconcile_attribute(
    row: CanonicalRow,
    vocab: str,
    allowed: list[str],
    ref: ReferenceData,
    stats: ReconcileStats,
    snappable: bool,
) -> bool:
    """Return True if the chosen value is in the allowed set (after an optional
    snap). On a genuine miss for a non-snappable identity attribute (colour) or
    when no allowed value exists, route a missing_leaf_child gap."""
    chosen = getattr(row, f"{vocab}_name", None)
    allowed_norm = {proj.norm(a) for a in allowed}

    if not chosen:
        # nothing chosen: only a gap if the variety requires this attribute and
        # has exactly one allowed value we could have used. Otherwise leave null.
        return True

    if proj.norm(chosen) in allowed_norm:
        return True

    if snappable and allowed:
        nearest, score = _nearest_allowed(chosen, allowed)
        if nearest and score >= 80:
            stats.snapped += 1
            setattr(row, f"{vocab}_name", nearest)
            setattr(row, f"{vocab}_method", f"snapped({score:.0f})")
            row.add_flag(
                ReviewFlag(
                    field=vocab,
                    code=FlagCode.leaf_snapped,
                    raw_value=chosen,
                    best_guess=nearest,
                    confidence=Confidence.medium,
                    method="snap",
                    src_url=row.src_url,
                )
            )
            return True

    # genuine miss: the variety lacks this finish/colour -> missing_leaf_child
    stats.missing_leaf += 1
    row.add_gap(
        TreeGap(
            src_site=row.src_site,
            surrogate_key=row.surrogate_key or "",
            raw_name=row.raw_name or "",
            normalized_name=proj.norm(row.variation_name or ""),
            suggested_type=row.type_name,
            suggested_color=row.color_name if vocab == "color" else None,
            suggested_finish=row.finish_name if vocab == "finish" else None,
            suggested_quality=row.quality_name if vocab == "quality" else None,
            gap_kind=GapKind.missing_leaf_child,
            nearest_existing=", ".join(allowed[:5]),
            example_src_url=row.src_url,
        )
    )
    return False


def _finalize_ids(row: CanonicalRow, ref: ReferenceData) -> None:
    """Map the (possibly snapped) names to ids and set the category pcat."""
    from stone_pipeline.stages.format_resolve import branch_of, category_pcat_for_branch

    row.color_id = _map_id(ref, "color", row.color_name)
    row.finish_id = _map_id(ref, "finish", row.finish_name)
    row.quality_id = _map_id(ref, "quality", row.quality_name)
    row.type_id = _map_id(ref, "type", row.type_name)
    row.category_pcat_id = category_pcat_for_branch(branch_of(row), ref)
    row.category_method = "branch"


def run(rows: list[CanonicalRow], ref: ReferenceData) -> ReconcileStats:
    stats = ReconcileStats()
    for row in rows:
        reconcile_row(row, ref, stats)
    log.info(
        "reconcile done",
        extra={
            "extra_fields": {
                "validated": stats.validated,
                "snapped": stats.snapped,
                "type_overridden": stats.type_overridden,
                "missing_leaf": stats.missing_leaf,
                "missing_variation": stats.missing_variation,
                "filled_from_variety": stats.filled_from_variety,
            }
        },
    )
    return stats
