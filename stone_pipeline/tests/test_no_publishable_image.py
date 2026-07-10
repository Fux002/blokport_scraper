"""The bad-image discard contract (full-pipeline side): Stage 7 (the image pipeline) may classify every
scraped image of a variant as non-stone and discard them, marking the row no_publishable_image. That is a
TERMINAL known-good empty, NOT a failure: the product publishes without an image (not rejected as no_image),
the discard reason surfaces in review, and it is never retried/alarmed as broken. The full pipeline only
reacts to the flag; the pixel decision + discard memory live entirely in Stage 7.
"""

from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.stages.validate import validate_row


def _row(discarded: bool) -> CanonicalRow:
    row = CanonicalRow(src_site="x")                 # no image_keys (empty by default)
    if discarded:
        # Stage 7's signal: every scraped image classified non-stone + discarded, with provenance.
        row.add_flag(ReviewFlag(field="images", code=FlagCode.no_publishable_image,
                                best_guess="spec sheet", method="clip_v1", src_url="http://x/spec.jpg"))
    return row


def test_no_publishable_image_publishes_not_rejected():
    row = _row(discarded=True)
    validate_row(row, require_images=True)
    # NOT rejected for a missing image -- a terminal known-good empty publishes imageless.
    assert not any(r.rule == "no_image" for r in row.reject_reasons)
    # the discard reason still surfaces in the review row (provenance carried on the flag).
    assert any(f.code == FlagCode.no_publishable_image and f.method == "clip_v1" for f in row.review_flags)


def test_transient_no_image_still_rejects_when_required():
    row = _row(discarded=False)                       # nothing scraped / download failed
    validate_row(row, require_images=True)
    assert any(r.rule == "no_image" for r in row.reject_reasons)   # transient -> reject, retry next scrape


def test_neither_rejects_when_images_not_required():
    row = _row(discarded=False)
    validate_row(row, require_images=False)
    assert not any(r.rule == "no_image" for r in row.reject_reasons)
