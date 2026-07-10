# Bad-image discard: the producer side (image pipeline) -- IMPLEMENTED

Pairs with `IMAGE_DISCARD_CONTRACT.md` (the consumer side, already live). That doc pins the flag Stage 7
must emit; this documents how the image pipeline PRODUCES it. Nothing here changes the consumer side.
Off by default: `BLOKPORT_IMAGE_CLASSIFY=false` until the margin is validated on a real sample (see below).
No em dashes anywhere (design principle 2).

## Architecture

Classification runs in the **GPU reprocess** (where torch already lives), NOT in Stage 7 / produce.
`:core` (the produce / Fargate image) stays torch-free. Consequence: a brand-new image's discard verdict
lands on the produce AFTER the next reprocess (one-cycle lag), which fits the two-phase flow (produce
re-hosts raw, GPU reprocess does the heavy ML).

- **GPU reprocess** (`io/image_processing.py` `_StoneClassifier`, `deploy/reprocess_source.py`): classify
  each scraped image; write a discard marker for each non-stone image; enhance only what is kept.
- **Stage 7** (`stages/images.py`, torch-free): read the discard pool; skip discarded urls; emit the
  terminal flag when a variant's every image is discarded.
- **Consumer** (`stages/validate.py`): reacts to the flag (already built).

## What shipped

### 1. Classifier -- `_StoneClassifier` (`io/image_processing.py`)
Torch-gated like `_Dewatermarker`: `available()` is true only when `torch` + `transformers` + the baked
CLIP weights load. Reuses `transformers.CLIPModel` (already pulled in by the de-watermark stack, so NO new
dependency); model `openai/clip-vit-base-patch32`, baked pinned by revision in the Dockerfile. Zero-shot:
softmax the image over a stone prompt set + a non-stone set (both in `ImageProcessingConfig`); discard iff
`P(non-stone) >= classify_margin` (default 0.85, conservative). Whole-image semantic score, NEVER text
density, so a slab photo WITH printed sizes or a small vendor logo stays a stone photo (low P(non-stone)).
Fail safe: no torch/weights -> `available()` False -> `ImageProcessor.classify()` returns keep=True, so the
pipeline can never blank the catalogue by accident. Deterministic (a forward pass, no sampling).

### 2. Discard memory -- content-addressed markers (`io/imagestore.py`)
One tiny object per discarded image: `products/discarded/<src>/<sha256>.json` = `{reason, score,
classifier}` (`discarded_key()`). One object per image (content-keyed) means the many parallel reprocess
slices never race a shared file. `sha_from_url()` recovers the sha from any hosted url/key -- the single
identity the reprocess (filename) and Stage 7 (improved url) share. Idempotent: same image -> same sha ->
same marker. Classify once, remembered across re-scrapes.

### 3. Reprocess writes markers (`deploy/reprocess_source.py`)
Per image: `classify()`; if non-stone, PUT the marker and `continue` (not enhanced, not published); else
de-watermark + enhance into `improved/` as before. New env `CLASSIFY=true` (default on) toggles it; the log
tally gains `discarded=N`.

### 4. Stage 7 reads the pool + emits (`stages/images.py`)
`_load_discard_set(cfg)` lists `products/discarded/` once (best-effort; `{}` offline / local mode) into a
sha set. Both the passthrough and s3/local paths skip a url whose embedded sha is in the set. A variant is
TERMINAL (`no_publishable_image`) only when EVERY scraped image is discarded -- none survived and none
merely held/failed (those stay transient `no_image`, retried). Exactly one flag per imageless row.

### 5. Cleanup is automatic
A discarded product image becomes unreferenced (Stage 7 stops linking it), so `deploy/cleanup_images.py`
prunes it on its normal catalog sweep. That tool sweeps `improved/` + `scraped/` ONLY, never the
`discarded/` markers, so the memory survives. NB: this is the scraped-photo pool, distinct from the FAL
`variations/{Key}.png` variant textures (a separate pool, untouched by discard). No ad-hoc `aws s3 rm`.

### 6. Config + deploy + tests
- `config/settings.py` `ImageProcessingConfig`: `classify`, `classify_model`, `classify_margin`,
  `classify_stone_prompts`, `classify_nonstone_prompts` (one config block, no inline tunables).
- `Dockerfile`: CLIP baked pinned (revision `3d74acf9...`) into the imageproc + gpu targets, `.msgpack/.h5`
  ignored. `requirements-imageproc.txt`: transformers already pinned (now dual-use).
- `stone_pipeline/tests/test_image_discard.py`: terminal/transient/partial emit (both modes), the
  discard-plus-failure transient guard, sha/marker helpers, classifier fail-safe, margin+reason math.

## The one prerequisite before enabling in prod (B1)
The margin cannot be guessed. Run `deploy/calibrate_classifier.py` (SRC=<source> SAMPLE=<n>) on the
imageproc/gpu image: it samples real `scraped/` images, prints `P(non-stone)` + reason sorted by
borderline-ness, and shows how many would be discarded at the current margin. Set `classify_margin` so the
split cleanly separates spec sheets / logos from slabs (including slabs-with-sizes, which must stay LOW).
Only then flip `BLOKPORT_IMAGE_CLASSIFY=true`.

## Invariants
Deterministic + idempotent (same discard set -> same emit; re-run byte-identical). Provenance on every
discard. Fail loud + isolated (a classify error on one image keeps that image, never crashes the slice).
Source isolation: all of this lives in the image pipeline; no `if source ==` in shared stages.
