# Bad-image discard: the full-pipeline / image-pipeline seam

The image pipeline (Stage 7 / GPU reprocess) may classify every scraped image of a variant as non-stone
(spec sheet, pure vendor logo) and discard them. This introduces one terminal state, `no_publishable_image`,
that the full pipeline reacts to WITHOUT ever inspecting pixels. Ownership is split cleanly:

- Image pipeline owns: the pixel classification, the durable discard set (content-hash + source-URL, in the
  image manifest, so a re-scrape skips re-classification), the provenance (classifier id, score, reason),
  and cleaning a now-stale `{Key}.png` via `deploy/cleanup_images.py` (products-only, guarded -- never
  ad-hoc `aws s3 rm`).
- Full pipeline owns: reacting to the state. Implemented here.

## What the full pipeline now does (implemented + tested)

- New flag `FlagCode.no_publishable_image` (`core/schema.py`).
- `stages/validate.py`: a row carrying that flag is a TERMINAL known-good empty. When images are required
  (`require_images`), it is NOT rejected as `no_image` -- it PUBLISHES without an image, exactly like a
  variant that legitimately never had a photo. A transient `no_image` (nothing scraped / download failed)
  still rejects and retries next scrape.
- Because the row publishes (is emittable), it is NOT counted as lost/discontinued and is never re-fetched
  as broken. Its `no_publishable_image` flag (with the discard reason) surfaces in the review CSV.
- Determinism: byte-identical until Stage 7 emits the flag (no row carries it today).

## The exact signal Stage 7 must emit (the other chat's side)

When Stage 7 discards ALL of a variant's scraped images as non-stone, for that row:

1. leave `row.image_keys` empty (the existing no-image path);
2. add the terminal flag with provenance, e.g.:

   ```python
   row.add_flag(ReviewFlag(
       field="images",
       code=FlagCode.no_publishable_image,
       best_guess="<reason, e.g. 'spec sheet'>",   # surfaced in review
       method="<classifier id, e.g. 'clip_v1'>",    # provenance
       confidence=<score-derived Confidence>,
       src_url="<a discarded source url>"))
   ```

3. do NOT also add `FlagCode.no_image` (that is the transient case). Emit exactly one of the two.

That is the entire coupling: Stage 7 sets the flag; the full pipeline reacts to the flag. The discard set,
provenance computation, and image cleanup remain entirely inside the image pipeline.
