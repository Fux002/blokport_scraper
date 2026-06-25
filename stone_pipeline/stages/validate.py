"""Stage 9: validation gate (section 7 Stage 9).

Reject a row on any hard failure: a required attribute id null (type, colour,
finish, quality), variation_id null, an unresolved leaf (a tree gap present),
category not the branch's valid category, handle or slug not globally unique, or
no image when images are required by config. Soft issues (review flags, no hard
failure) emit or hold per the emit_on_review switch.

Hard rejects never reach the import CSV regardless of the switch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stone_pipeline.config.settings import active_categories
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, RejectReason

log = logfmt.get_logger("validate")

# required attribute ids for a row to be importable (section 7 Stage 9, 6A)
REQUIRED_ID_FIELDS = ("type_id", "color_id", "finish_id", "quality_id", "variation_id")


@dataclass
class ValidationResult:
    emit: list[CanonicalRow] = field(default_factory=list)
    review_only: list[CanonicalRow] = field(default_factory=list)
    rejects: list[CanonicalRow] = field(default_factory=list)


def validate_row(row: CanonicalRow, require_images: bool = False) -> None:
    for id_field in REQUIRED_ID_FIELDS:
        if not getattr(row, id_field, None):
            row.add_reject(RejectReason(rule="required_id_null", detail=id_field))
    if row.tree_gaps:
        kinds = ",".join(sorted({str(g.gap_kind) for g in row.tree_gaps}))
        row.add_reject(RejectReason(rule="tree_gap", detail=kinds))
    # a row may only emit under an ACTIVE category's pcat (registry); a category
    # whose pcat is empty (e.g. tiles until supplied) cannot emit yet.
    valid_pcat = {c.pcat_id for c in active_categories()}
    if row.category_pcat_id not in valid_pcat:
        row.add_reject(RejectReason(rule="category_invalid", detail=str(row.category_pcat_id)))
    if not row.handle or not row.slug:
        row.add_reject(RejectReason(rule="handle_missing", detail=""))
    if require_images and not row.image_keys:
        row.add_reject(RejectReason(rule="no_image", detail=""))
    # Bad source data: a SIZE that was present in the scrape but invalid (<= 0, e.g. a "0cm" typo)
    # is left <= 0 by derive (never fabricated over). Reject the product rather than sell it with a
    # wrong size that breaks the category's area/volume pricing. (Absent sizes are synthesised > 0.)
    for dim in ("length", "width", "height"):
        if (getattr(row, dim, None) or 0) <= 0:
            row.add_reject(RejectReason(rule="dimension_invalid", detail=dim))
            break


def run(rows: list[CanonicalRow], emit_on_review: bool, require_images: bool = False) -> ValidationResult:
    result = ValidationResult()
    handles: dict[str, str] = {}
    for row in rows:
        validate_row(row, require_images=require_images)
        # global uniqueness of handle/slug after namespacing (system bug if not)
        if row.handle:
            if row.handle in handles and handles[row.handle] != row.surrogate_key:
                row.add_reject(RejectReason(rule="handle_collision", detail=row.handle))
            handles.setdefault(row.handle, row.surrogate_key)

    for row in rows:
        if not row.is_emittable:
            result.rejects.append(row)
        elif row.review_flags and not emit_on_review:
            result.review_only.append(row)
        else:
            result.emit.append(row)

    log.info("validate done", extra={"extra_fields": {
        "emit": len(result.emit), "review_only": len(result.review_only), "rejects": len(result.rejects)}})
    return result
