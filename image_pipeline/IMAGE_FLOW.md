# Variant image flow — generate → de-background → upload

Closes the image loop so a variant's picture exists at the exact URL Medusa reads:
`https://blokport-dev-staging-3e58a6.s3.eu-west-1.amazonaws.com/dev/variations/{Key}.png`

## The full circle

```
scrape ─▶ catalog sync ─▶ new variants + "images_to_generate"
                              │
   (1) build prompts         ▼
   python -m stone_pipeline.stages.image_prompts
        → image_pipeline/prompts_to_generate.json   (output_name = {Key})
                              │
   (2) generate (fal.ai)     ▼     MODEL = "max"
   cd image_pipeline && python genetate_images.py
        → ./images/{Key}.png        (512px, white bg, slab geometry preserved)
                              │
   (3) remove background     ▼     BEN2
   python rb_images.py
        → ./to_upload/{Key}.png     (name unchanged — already the Key)
                              │
   (4) upload                ▼
   aws s3 cp ./to_upload/ s3://blokport-dev-staging-3e58a6/dev/variations/ --recursive
                              │
   (5) variant files already point at dev/variations/{Key}.png ─▶ load into Medusa
```

Steps 2–4 are the paid / heavy / outward-facing parts; 1 and 5 are wired into the pipeline.

## Only product-backed images are generated
A new variety is added to every active category for a uniform catalog, but an image
is generated ONLY for the category a scraped product was actually observed in
(`product_backed`). A slab-only supplier does NOT trigger a block image with no
product. So `images_to_generate.csv` (and the prompts file) hold only the
product-backed variants — e.g. 105 images, not 206 — and fan-out copies stay
imageless until a product uses them.

## Dev AND prod: same images, different bucket
The `{Key}.png` files are identical across environments. So:
- Upload `to_upload/` (and `images/renamed/`) to BOTH the dev and prod buckets
  under `.../variations/`.
- The variant file differs ONLY by the bucket base. Produce the prod set by
  re-running the catalog with the prod bucket env var (it stamps the base into
  `to_upload/1_variants_full.csv`):
  `BLOKPORT_VARIANT_IMAGE_BASE="https://<prod-bucket>/.../variations/" \`
  `    python -m stone_pipeline.catalog`

## Setup (once)
```
pip install fal-client requests pillow            # generator
pip install torch "ben2==0.0.1"                   # background remover (BEN2) — PIN the version
```
> Supply-chain note: `ben2` is a small external package (not in the deployed scraper
> image) and `BEN_Base.from_pretrained("PramaLLC/BEN2")` downloads model weights from
> Hugging Face. Pin the version (above), and treat this generation feature as gated /
> run it in an isolated environment — it is NOT part of the Fargate scraper deploy.
- The fal key comes from the environment: `export FAL_KEY="<id>:<secret>"` (on AWS it is injected from SSM SecureString /blokport-<env>/FAL_KEY). Rotate it as needed.
- `MODEL = "max"` is already set (best fidelity). 206 prompts ≈ **$21** at ~$0.10/img.
- BEN2 runs on CPU here (slow but fine); a GPU box is far faster for a big batch.

## Run
```
python -m stone_pipeline.stages.image_prompts          # (re)build prompts from the latest catalog
cd image_pipeline
python genetate_images.py                              # ./images/{Key}.png   (resumable)
python rb_images.py                                    # ./to_upload/{Key}.png
aws s3 cp ./to_upload/ s3://blokport-dev-staging-3e58a6/dev/variations/ --recursive
```
Both scripts are resumable (skip files already produced), so re-running continues a partial batch.

## Prompt-builder modes (all write the same prompts_to_generate.json)
```
python -m stone_pipeline.stages.image_prompts              # new product-backed variants (normal)
python -m stone_pipeline.stages.image_prompts --keys K1 K2 # just these variant Keys (fix a few)
python -m stone_pipeline.refresh_images                    # one-time quality refresh, product-backed only
```
`output_name` is the variant Key, so every mode overwrites `{Key}.png` in place — one image per
variant, never a new name. Need the fal key first: `export FAL_KEY=$(aws ssm get-parameter
--name /blokport-<env>/FAL_KEY --with-decryption --query Parameter.Value --output text)`.

## Refresh the poor-quality images (one-time, product-backed only, cost-gated)
Many live images were made by an earlier, worse model. `refresh_images` re-makes each PRODUCT-BACKED
variant's image ONCE with the current best model, and only those (a variant with no product does not
need a fresh texture). A durable S3 marker (`<env>/variations/_refreshed.json`) records every Key already
refreshed, so a re-run never regenerates -- or re-charges -- the same image twice.
```
python -m stone_pipeline.refresh_images                 # DRY RUN: how many + est. cost, spends nothing
BLOKPORT_REFRESH_APPLY=1 python -m stone_pipeline.refresh_images   # generate + upload + mark
```
- Selection = product-backed AND already-imaged AND not-yet-refreshed. It writes the queue, runs the
  generator + rb + upload (reused runners), overwrites each `{Key}.png` in place, then adds the Keys to
  the marker. Imageless variants are build()'s new-image queue, not a refresh.
- **Cost:** product-backed set (~362) x ~$0.10/img `max` tier ≈ **~$36** one-time. The marker guarantees
  you never pay twice for the same image. Needs `fal_client` + `torch` + `FAL_KEY`; on a box without the
  stack the dry run still reports the plan.

## Notes / caveats
- **Base image per category:** each prompt carries its own `base_image_url` — slab
  variants edit the slab base, block variants the block base, so blocks look like
  blocks. The two URLs live in `SETTINGS.curation.variant_base_image_{slab,block}`.
- **Existing 12,471 images** were already renamed to `{Key}.png` (in `images/renamed/`);
  upload those to `dev/variations/` too so every variant resolves.
- **Parked variants** (`catalog_source/missing_variants.csv`) are intentionally NOT
  generated (no/ambiguous image); generate + un-park them later if wanted.
- **Dev → prod:** only the bucket base differs. Point the same `{Key}.png` files at
  the prod bucket and set `SETTINGS.curation.variant_image_base` accordingly.

## Future automation (optional)
- Wrap steps 2–4 in one `python -m stone_pipeline.images generate` once deps are pinned.
- Push `to_upload/` to S3 from `io/storage.py` (the S3 backend already exists).

---

# Scraped PRODUCT photos (separate pipeline)

Everything above is about the generated **variant** texture image (`{Key}.png`).
This is a different concern: the **product** photos a scraper captures — the real
slabs a customer buys, usually shot in a storage unit under poor, uneven light.

Where it runs: **Stage 7**, `stone_pipeline/stages/images.py`, in the `local`/`s3`
image modes (passthrough never downloads bytes). With `SCRAPER_IMAGE_PROCESSING=true`
each new source image is size-reduced (long edge capped, never enlarged; CPU) and
re-hosted under `improved/`, its raw original kept under `scraped/`. It is keyed on the
*source* bytes' hash, so a re-run re-uploads nothing.

A source with its `image_pipeline` on (admin, default) then gets one standardised photo
per listing from the photo process (`STANDARDISE_PROCESS.md`): a GPU standardise, a
hosted judge, and a hosted clean of reflections, marks and burned-in logos the judge
finds. With it off, its products show the re-hosted scraped photos.
