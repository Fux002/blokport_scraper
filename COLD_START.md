# Cold start: prerequisites and fail-loud guards

A "cold start" = producing into an empty/clean ledger + a clean Medusa. Several things the pipeline
depends on are NOT created by the cold start itself; if they are missing the produce used to exit 0 with
an empty catalogue (silent). This documents the prerequisites and the guards that now make a broken cold
start fail LOUD instead.

## Operational prerequisites (must exist BEFORE a cold start)

1. **The attribute vocabulary must already exist in Medusa AND in the S3 attributes export.**
   The scraper does not send attributes -- there is no attributes lane in the sync. Products carry
   colour/finish/quality/**type** as NAMES, which Medusa resolves against its existing attribute
   definitions. Typing is a HARD serve gate (an untyped variety never serves, and neither do its
   products), and the type vocabulary is seeded only from the Medusa attributes export
   (`attributes.csv`). So:
   - If a "clean Medusa" also wipes the attribute definitions, products cannot resolve.
   - If the S3 attributes export is empty/absent, nothing types and nothing serves.
   New values discovered mid-scrape are the known dormant C3 case (held for review, not auto-sent) --
   they are created via the new-variant review "attributes" screen, not by the cold start.

2. **FAL must be wired to the produce (SCRAPER-side only).** Every product is held until its variety's
   `{Key}.png` is on S3. On a cold start that is thousands of new textures, generated inside the produce
   only when `FAL_KEY` is set (via `produce_secret_arns`). Without it, variations sync but ZERO products
   serve. This is entirely the scraper's concern -- the produce generates the texture and uploads it to
   S3; Blokport/Medusa never generates or supplies a variant image, it only reads the `image_url`.
   FAL_KEY is wired on dev (the sync_service task); it must be wired per environment (scraper infra).

## Fail-loud guards (added so a stalled cold start is not mistaken for success)

- **Typed-0 / empty-vocab is FATAL** (`produce._cold_start_stall`, risks 1/4/5). After the build, if the
  type vocabulary is empty OR 0 of the produced variations are typed, the produce fails with a red
  `produce FATAL: cold-start stall ...` and a non-zero exit, instead of holding the entire catalogue
  silently. This runs regardless of the consistency gate's own result (a typed-0 run can pass the gate
  with 0 products and look clean). A FEW untyped varieties (the normal pass-1 hold) is not a stall.
- **Missing textures are surfaced LOUDLY** (`catalog._auto_queue_images`, risk 2). If new-variant
  textures are queued but `FAL_KEY` is not set for the produce, a WARNING states that N textures were not
  generated and their products stay HELD -- so an empty product lane is not read as a clean run.

## Known minor

- ~15 of ~24,735 varieties have Keys with no recoverable type slug (e.g. `slab_alpine_luxe_...`), so they
  are held untyped forever (risk 6). This is a mint-time data issue, not a flow bug; they never serve.
  The stall guard is unaffected (typed > 0 overall), and `typing_health` reports `produced - typed` so
  the residual untyped count stays visible.

## Blokport / Medusa side (ONE thing)

- The **clean-reset must NOT delete the attribute definitions** (colour / finish / type / quality). The
  scraper depends on them pre-existing; "zero" means zero sync state, not zero attribute vocabulary.

(FAL / texture generation is NOT a Blokport concern -- it is entirely scraper-side, see prerequisite 2.)
