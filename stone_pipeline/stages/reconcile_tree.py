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
from stone_pipeline.stages._rowguard import isolate_rows
from stone_pipeline.matching import projections as proj
from stone_pipeline.adapters.tokens import recognize_type
from stone_pipeline.reference.loaders import ReferenceData, type_slug_from_key

log = logfmt.get_logger("reconcile_tree")

@dataclass
class ReconcileStats:
    validated: int = 0
    snapped: int = 0
    type_overridden: int = 0
    missing_leaf: int = 0
    missing_variation: int = 0
    filled_from_variety: int = 0
    isolated: int = 0   # rows dead-lettered on an unexpected exception (stages/_rowguard.py)


# Colour/quality MAY be filled from the matched variety when the scrape omits them
# (the backbone variety is the authority, so it is not a guess). Finish is deliberately
# NOT: a variety allows many finishes and finish is identity-bearing for the product.


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

    # The variation's TYPE is the single authority and is intrinsic to the variation the MATCHER bound
    # (encoded in variation_key) -- never a name-derived type_name, never a FOREIGN same-name backbone
    # record of another stone. A product name can carry a stray type word ('Tiger Black Marble Slab', a
    # granite) and a product can mis-bind across a name collision or base<->backbone drift ('Azul White'
    # is a quartzite in the export but only an onyx in the backbone). In every case the type of the
    # variation a product is attached to is what its Key says. Pinning it here keeps type and variation on
    # the SAME variety, so a product can never price or ship as a stone different from the variety it links
    # to (which tree_build would otherwise absorb into a self-consistent but WRONG cross-type combination).
    bound_type, type_from_key = _bound_type(row)
    _apply_type(row, bound_type, ref, stats, from_key=type_from_key)

    # Membership record: the backbone variety of this name AND the bound type, for its colour/finish/
    # quality sets. A foreign same-name record of a DIFFERENT type is never used -- it would validate
    # against the wrong stone's sets. Absent (base<->backbone drift): keep the pinned type, flag the
    # missing record, do not fabricate membership.
    variety = ref.backbone.lookup(row.variation_name or "", stone_type=bound_type)
    if variety is None:
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


def _bound_type(row: CanonicalRow) -> tuple[str, bool]:
    """(type, from_key). The canonical stone type of the variation the matcher bound, read from its Key
    (the type authority), and whether it GENUINELY came from the Key. type_slug_from_key takes the LONGEST
    known type slug so multi-word types resolve ('slab_dolomite_marble_..' -> 'Dolomite Marble'). Falls
    back to the Stage-3 resolved (name-derived) type only when there is no variation_key or the Key carries
    no known type -- that fallback is NOT variation-authoritative (from_key=False), so downstream (origin)
    must not trust it for the curated (name, type) lookups."""
    if row.variation_key:
        canon = recognize_type(type_slug_from_key(row.variation_key))
        if canon:
            return canon, True
    return (row.type_name or row.raw_type or ""), False


def _apply_type(row: CanonicalRow, new_type: str, ref: ReferenceData, stats: ReconcileStats,
                *, from_key: bool) -> None:
    if not new_type:
        return
    # Only a KEY-authoritative type genuinely OVERRIDES a name-derived one; a fallback that merely echoes
    # the name-derived type is not an override, and must not claim variety-authoritative provenance.
    if from_key and row.type_name and proj.norm(row.type_name) != proj.norm(new_type):
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
    # id mapping is owned by _finalize_ids (always runs after this on every reconcile_row path); it maps
    # row.type_name, which we just set, so setting type_id here too would be a redundant second mapping.
    if from_key:
        row.type_method = "variety_authoritative"
        row.type_confidence = Confidence.high.name
    else:
        # State 2: the variation bound but its Key carries no known type slug, so this is the name-derived
        # type kept as a best-guess fallback -- NOT variation-authoritative. Mark it so derive_origin will
        # not trust it for the curated (name, type) origin lookups (which would confidently resolve a
        # homonym's WRONG origin). The type VALUE stays (best available); only its provenance is honest.
        row.type_method = "type_name_fallback"
        row.type_confidence = Confidence.low.name


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
        # nothing chosen for this (optional) attribute: leave it null, never gap on it.
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
    stats.isolated = isolate_rows(rows, "reconcile", lambda r: reconcile_row(r, ref, stats), log)
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
                "isolated": stats.isolated,
            }
        },
    )
    return stats
