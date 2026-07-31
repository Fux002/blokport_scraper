"""Stage 9: validation gate (section 7 Stage 9).

Reject a row on any hard failure: a required attribute id null (type, colour,
finish, quality), variation_id null, an unresolved leaf (a tree gap present),
category not the branch's valid category, handle or slug not globally unique, a
missing owner (company_id / sales_channel_id, required at all times), or no image
when images are required by config -- EXCEPT a row Stage 7 marked no_publishable_image,
a terminal known-good empty that publishes without an image. Soft issues (review
flags, no hard failure) emit or hold per the emit_on_review switch.

Hard rejects never reach the import CSV regardless of the switch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stone_pipeline.config.settings import active_categories
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, RejectReason

log = logfmt.get_logger("validate")

# required attribute ids for a row to be importable (section 7 Stage 9, 6A)
REQUIRED_ID_FIELDS = ("type_id", "color_id", "finish_id", "quality_id", "variation_id")


def _no_publishable_image(row: CanonicalRow) -> bool:
    """True when Stage 7 (the image pipeline) classified every scraped image as non-stone and discarded
    them -- a TERMINAL known-good empty, not a transient no_image. Such a product is a real stone with no
    usable photo: it publishes WITHOUT an image (like a legitimately imageless variant), the discard reason
    surfaces in review, and it is never rejected/retried/alarmed as broken. The pixel decision + the discard
    memory live entirely in Stage 7 (source-isolation preserved); the full pipeline only reacts to the flag."""
    return any(f.code == FlagCode.no_publishable_image for f in row.review_flags)


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
        row.add_reject(RejectReason(rule="handle_missing", detail="handle" if not row.handle else "slug"))
    # Owner ids are REQUIRED at all times: sales_channel makes a product visible/buyable, company owns it.
    # constants (Stage 8) sets them from config (dev has defaults; prod must set the env vars). A blank
    # here means a misconfigured owner -- reject rather than emit an unowned, channel-less (invisible)
    # product. This is the row-level gate every emit path shares (the run.main prod guard is the early,
    # whole-run version of the same rule).
    if not row.company_id or not row.sales_channel_id:
        row.add_reject(RejectReason(rule="owner_missing",
                                    detail="company_id" if not row.company_id else "sales_channel_id"))
    # No usable image + images required -> reject as a transient no_image (retry next scrape). EXCEPTION:
    # a row Stage 7 marked no_publishable_image is a terminal known-good empty -- it publishes WITHOUT an
    # image instead of being rejected, so a real stone whose only photos were spec sheets/logos is not lost.
    if require_images and not row.image_keys and not _no_publishable_image(row):
        row.add_reject(RejectReason(rule="no_image", detail=""))
    # A dimension whose source FETCH FAILED (recoverable) is HELD as a distinct TRANSIENT reject -- "retry
    # me next scrape", not "bad data" -- and is exempt from the dimension_invalid check below so the hold
    # reads cleanly. Retries exactly like no_image (a fresh scrape re-derives; it emits once the fetch
    # succeeds). derive left these None + flagged dimension_unavailable; it never defaulted a fabricated size.
    unavailable = {f.field for f in row.review_flags if f.code == FlagCode.dimension_unavailable}
    if unavailable:
        row.add_reject(RejectReason(rule="dimension_unavailable", detail="|".join(sorted(unavailable))))
    # A genuinely-absent dimension was filled from the pack DEFAULT in derive (flagged dimension_defaulted).
    # A fabricated size feeds area/volume pricing + freight, so it must NOT ship: HOLD it for review like an
    # unavailable one (the operator supplies a real size before the product can be sold). No guessed freight
    # basis reaches a live order.
    defaulted = {f.field for f in row.review_flags if f.code == FlagCode.dimension_defaulted}
    if defaulted:
        row.add_reject(RejectReason(rule="dimension_defaulted", detail="|".join(sorted(defaulted))))
    # Dimensions are REQUIRED: a genuinely-absent one was defaulted in derive (flagged), so a size that is
    # still <= 0 / None here is a data error -- reject it (a wrong real size breaks area/volume pricing and
    # freight). (`None or 0` -> 0 <= 0, so a missing size rejects too.) Skip a fetch-failed dim (held above).
    for dim in ("length", "width", "height"):
        if dim in unavailable:
            continue
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
