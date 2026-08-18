"""Global configuration block for the stone import pipeline.

Operating principle 1: no argparse. All paths, ids, thresholds, and tunables
live here (global defaults) or in config/sources.yaml (per source). Nothing is
inlined in stage or resolver code.

Operating principle 8: the template is the schema authority. Column names and
order are read from the live template at emit time, never hardcoded here. This
file only points at where that template lives.

All backend ids below belong to one backend environment (section 3.2). The
defaults are the dev-staging values observed in the real upload example; a real
run must confirm them against the live backend fingerprint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from enum import IntEnum
from pathlib import Path

from stone_pipeline.config.domain import active_pack  # dependency-free (yaml only); no import cycle

# Repository root: this file is stone_pipeline/config/settings.py, so two parents up.
REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent


# --- Deployment environment (AWS development vs production) --------------------
# The whole pipeline deploys to AWS, with separate DEVELOPMENT and PRODUCTION
# setups. BLOKPORT_ENV selects which; everything environment-specific (S3 bucket,
# the URL/key path segment, dry-run, image mode, processing) derives from it or
# from a matching env var, so promotion dev -> prod is a CONFIG change (env vars
# on the ECS task / Lambda / Batch job) and never a code edit. The defaults below
# reproduce the current dev-staging behaviour exactly, so nothing changes until
# the env vars are set. See DEV_PROD_PIPELINE.md for the promotion checklist.
BLOKPORT_ENV = os.environ.get("BLOKPORT_ENV", "development").strip().lower()
IS_PRODUCTION = BLOKPORT_ENV in ("production", "prod")
# Two DELIBERATELY separate S3 top-level prefixes per environment, split by lifecycle (not by accident --
# each is owned by a different subsystem, and the boundary is REGENERABLE-vs-DURABLE):
#   ENV_SEGMENT (dev|prod)          -- the REGENERABLE data + integration plane. products/, variations/,
#                                      scraper/{to_upload,from_medusa,review,texture_queues}. A produce
#                                      rebuilds all of it and Blokport/Medusa read it; a factory reset may
#                                      wipe it (that is why a reset empties dev/products/ but nothing else).
#   ENV_NAME (development|production) -- the local workspace folder AND the S3 namespace for DURABLE state
#                                      that must survive a cold task: scraper/{config/config.db, ledger/<env>.db,
#                                      artifacts/*.tar.gz}. A produce does NOT regenerate these; the snapshot
#                                      layer (ledger/snapshot.py) owns this prefix, reading+writing it consistently.
# Same rule in prod (prod/ + production/), in a SEPARATE guarded bucket. See DEPLOY.md "S3 layout".
ENV_SEGMENT = "prod" if IS_PRODUCTION else "dev"
ENV_NAME = "production" if IS_PRODUCTION else "development"

# Dev and prod hold SEPARATE Medusa downloads and upload sets, because the Medusa ids
# (pcat / attribute / variation) differ per environment. What is SHARED is the "core":
# the raw scrape (data/) and the hand-maintained catalog_source/ (names, not ids). So
# you scrape once, then run the catalog/tree per env (BLOKPORT_ENV) to get each set.
_FROM_MEDUSA = WORKSPACE_ROOT / "from_medusa" / ENV_NAME   # the env's Medusa downloads
_TO_UPLOAD = WORKSPACE_ROOT / "to_upload" / ENV_NAME       # the env's upload files
_REVIEW = WORKSPACE_ROOT / "review" / ENV_NAME             # the env's look-before-upload
_OUTPUTS = REPO_ROOT / "outputs" / ENV_NAME                # the env's per-source staging


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var (1/true/yes/on); fall back to default when unset."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; fall back to default when unset or non-numeric (fail soft to the default,
    never crash config import on a bad override). Parse with int() itself, not an isdigit() pre-check:
    isdigit() is True for a double sign ('--3', once dashes are stripped) and for Unicode digits ('3', '²'),
    both of which int() then REJECTS -- so the heuristic let malformed values through to a crash at import."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env var; fall back to default when unset or non-numeric (fail soft, never crash)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


# The dev bucket is known. The PROD bucket is supplied at deploy time via BLOKPORT_S3_BUCKET.
# In PRODUCTION we must NOT silently fall back to the dev bucket (that would store prod images in
# dev and stamp prod Medusa rows with dev URLs) -- fail fast instead. Dev keeps the known default.
_DEV_S3_BUCKET = "blokport-dev-staging-3e58a6"
_S3_BUCKET_ENV = os.environ.get("BLOKPORT_S3_BUCKET", "").strip()
if IS_PRODUCTION and not _S3_BUCKET_ENV:
    raise RuntimeError(
        "BLOKPORT_S3_BUCKET must be set in production — refusing to default to the dev bucket "
        f"({_DEV_S3_BUCKET}). Set BLOKPORT_S3_BUCKET to the prod bucket before running prod.")
S3_BUCKET = _S3_BUCKET_ENV or _DEV_S3_BUCKET
S3_REGION = os.environ.get("BLOKPORT_S3_REGION", "eu-west-1")


# Owner Medusa ids -- operational config, NOT catalog attributes, so they are environment
# variables (the catalog attribute ids -- color/finish/type/quality/category -- come from the
# env's attributes.csv instead). The sales channel is ONE id per environment; the company is
# the general Blokport owner by default and can be overridden per scrape in sources.yaml. Each
# has a dev default for local runs; a PRODUCTION run must set the env var -- it never falls back
# to a dev id (returns "" so the miss is loud, never a silent dev/prod cross).
_DEV_SALES_CHANNEL_ID = "sc_01KTM2B2DJNSW6WPS1Q8FN8B2R"
_DEV_COMPANY_ID = "01KTV98X8RG743YR3QHCECZKKA"


def _owner_id(env_var: str, dev_default: str) -> str:
    val = os.environ.get(env_var, "").strip()
    if val:
        return val
    return "" if IS_PRODUCTION else dev_default


SALES_CHANNEL_ID = _owner_id("BLOKPORT_SALES_CHANNEL_ID", _DEV_SALES_CHANNEL_ID)
COMPANY_ID = _owner_id("BLOKPORT_COMPANY_ID", _DEV_COMPANY_ID)

# Last-resort attribute defaults are DOMAIN VALUES, so they live in the active pack, not here:
# config/domains/<pack>.yaml -> last_resort_finishes / last_resort_quality / block_finish (read via
# domain.active_pack() in stages/normalize.py). Field NAMES stay the Medusa contract; the VALUES are pack.
# Colour is NOT last-resorted here (it inherits the variety's texture-classified colour, 'Natural' floor).


# --- Confidence enum with a fixed numeric mapping (section 3.4) ----------------
class Confidence(IntEnum):
    """Four level confidence, high > medium > low > none, with numeric backing
    so thresholds compare uniformly across every resolver."""

    none = 0
    low = 1
    medium = 2
    high = 3


@dataclass(frozen=True)
class Paths:
    repo_root: Path = REPO_ROOT
    workspace_root: Path = WORKSPACE_ROOT
    synonyms_dir: Path = REPO_ROOT / "reference" / "synonyms"
    state_dir: Path = REPO_ROOT / "state"
    fixtures_dir: Path = REPO_ROOT / "adapters" / "fixtures"
    # Live scrape outputs (scrapers write here): data/<source>/<timestamp>/products.csv
    data_dir: Path = WORKSPACE_ROOT / "data"
    # Top-level workspace folders, grouped by what you DO with each file. SHARED across
    # environments: data/ (the scrape) and catalog_source/ (hand-maintained backbones,
    # names not ids). PER-ENV (development/ vs production/, selected by BLOKPORT_ENV):
    #   to_upload/<env>/   - PRODUCED by the pipeline; upload these to that env's Medusa
    #   from_medusa/<env>/ - SAVE that env's Medusa downloads here; the pipeline READS them
    #   review/<env>/      - look before uploading; never uploaded
    #   outputs/<env>/     - internal per-source staging (canonical + products)
    catalog_source_dir: Path = WORKSPACE_ROOT / "catalog_source"   # SHARED
    to_upload_dir: Path = _TO_UPLOAD
    from_medusa_dir: Path = _FROM_MEDUSA
    review_dir: Path = _REVIEW
    outputs_dir: Path = _OUTPUTS
    # small CSV samples the test suite reads (self-contained in the package).
    tests_fixtures_dir: Path = REPO_ROOT / "tests" / "fixtures"

    # attribute name -> Medusa id; lives in from_medusa/<env>/ because its ids come FROM
    # that env's Medusa (like the variants export), not hand-maintained like catalog_source/.
    attributes_csv: Path = _FROM_MEDUSA / "attributes.csv"
    # The combined cross-category id-variants EXPORT (with Medusa Id), download-only:
    # the immutable "existing variants" the catalog reads. The UPLOAD files are PRODUCED
    # by the catalog (to_upload/) and never read back -- input and output never alias.
    variants_export_csv: Path = _FROM_MEDUSA / "variants_export.csv"
    # The committed, Id-free SOURCE OF TRUTH: the complete variant set (base == the full catalog). The
    # catalog seeds from THIS, not the live export -- so a cold start (Medusa emptied -> thin live export)
    # still rebuilds the whole catalog and only grows from there. sync_variants_base keeps it == full.
    variants_export_base_csv: Path = _FROM_MEDUSA / "variants_export_base.csv"
    # PRISTINE, read-only copy of the base, baked at image build (Dockerfile) and never written at runtime
    # (the live base above self-mutates: base := 1_variants_full each produce). The factory reset reconciles
    # the ledger against THIS so a seed cleanup actually reaches the running ledger. Absent locally -> the
    # committed base itself is pristine, so lifecycle falls back to it.
    variants_export_base_seed_csv: Path = _FROM_MEDUSA / "variants_export_base.seed.csv"

    # per-category backbone files derive from the CATEGORIES registry.
    @property
    def backbone_json(self) -> Path:        # legacy alias (== slab backbone)
        return self.backbone_slabs_json

    @property
    def backbone_slabs_json(self) -> Path:
        return category("slab").backbone_path

    @property
    def backbone_blocks_json(self) -> Path:
        return category("block").backbone_path

    @property
    def backbone_tiles_json(self) -> Path:
        return category("tile").backbone_path
    # ports.csv is supplied by the user into catalog_source; loader falls back to reference/.
    ports_csv: Path = WORKSPACE_ROOT / "catalog_source" / "ports.csv"
    ports_csv_fallback: Path = REPO_ROOT / "reference" / "ports.csv"
    units_csv: Path = REPO_ROOT / "reference" / "units.csv"
    # origin_map is HAND-MAINTAINED in catalog_source/ (variety -> country + geographic patterns),
    # like ports.csv -- it is NOT generated from any export. Fallback to reference/ for back-compat.
    origin_map_csv: Path = WORKSPACE_ROOT / "catalog_source" / "origin_map.csv"
    origin_map_csv_fallback: Path = REPO_ROOT / "reference" / "origin_map.csv"
    # Per-SUPPLIER origin overrides: (source, variety, stone_type) -> ISO2, for a supplier that sells a
    # stone it does NOT quarry in its home country (a reseller). Scoped to one source, so it never leaks
    # to other suppliers. Optional -- absent file = no overrides.
    origin_overrides_csv: Path = WORKSPACE_ROOT / "catalog_source" / "origin_supplier_overrides.csv"
    country_codes_csv: Path = REPO_ROOT / "reference" / "country_codes.csv"
    placeholder_hashes_csv: Path = REPO_ROOT / "reference" / "placeholder_hashes.csv"
    standard_slab_area_csv: Path = REPO_ROOT / "reference" / "standard_slab_area.csv"
    # per-stone-type material density (kg/m3): derive weight from real dimensions when a source has no
    # weight. Seeded from Medusa's type table; default row (Marble) is the rare fallback for a new type.
    type_density_csv: Path = REPO_ROOT / "reference" / "type_density.csv"

    # The import template defines the emit schema (operating principle 8). Lives in
    # the package (header is the schema authority).
    template_csv: Path = REPO_ROOT / "reference" / "medusa_import_template.csv"
    upload_sample_csv: Path = REPO_ROOT / "tests" / "fixtures" / "marenostone_bootstrap_200_final.csv"

    # Config files
    sources_yaml: Path = REPO_ROOT / "config" / "sources.yaml"
    source_contracts_yaml: Path = REPO_ROOT / "config" / "source_contracts.yaml"

    # Medusa product export (handle/SKU/inventory) the user downloads, so the
    # pipeline can tell new vs existing products and inventory changes (item 4/5).
    products_known_csv: Path = _FROM_MEDUSA / "products_export.csv"

    # State
    baselines_json: Path = REPO_ROOT / "state" / "scrape_baselines.json"
    # per-source medians of the CANONICAL magnitude fields (weight/dims) at last-good, for the
    # post-derive magnitude-drift gate (self-tuning, one layer below the raw scrape baseline above).
    magnitude_baselines_json: Path = REPO_ROOT / "state" / "magnitude_baselines.json"
    overrides_csv: Path = REPO_ROOT / "state" / "manual_overrides.csv"

    @property
    def export_file(self) -> Path:
        """The one combined id-variants export (with Medusa Id) for every category."""
        return self.variants_export_csv


@dataclass(frozen=True)
class Thresholds:
    """Section 3.4. Single source of truth for all matching and derivation."""

    variation_auto_accept: float = 92.0
    variation_review_floor: float = 84.0  # band 84..92 routes to review
    attribute_fuzzy_floor: float = 90.0
    # (The in-stock made-to-order fallback COUNT moved to the domain pack as `in_stock_fallback_qty`, a
    # per-category value -- a block is 1 piece, a slab/tile a modest quantity -- replacing the old single 999.)
    health_fill_drop_warn: float = 0.15
    health_fill_drop_fail: float = 0.40
    health_rowcount_floor: float = 0.50
    # Per-stage diagnostic DEGRADED thresholds (share of the batch). Informational only -- a DEGRADED
    # stage never changes routing (the gates remain the sole authority on what emits); it flags a
    # systemic anomaly at its own layer so a drift surfaces where it is born, not three stages later.
    normalize_unresolved_degraded: float = 0.25   # rows with an unresolved required attribute
    match_unmatched_degraded: float = 0.50        # rows with no variation match (majority -> systemic)
    derive_incomplete_degraded: float = 0.25      # rows missing a required derived dimension/weight
    validate_reject_degraded: float = 0.50        # rows rejected at validate (majority -> systemic, not stray)
    images_no_image_degraded: float = 0.25        # rows with no publishable image (held from shipping)


@dataclass(frozen=True)
class Admission:
    """The rule that decides when a data source is 'consistent enough to be allowed in' (auto-load). All
    config, no magic numbers in code. A source becomes ELIGIBLE for auto after `consistent_runs` clean runs
    in a row (health OK + no drift) AND (if required) passing certification; a drift event on an `auto`
    source auto-DEMOTES it back to review. Env-overridable for a different risk posture."""
    consistent_runs: int = _env_int("BLOKPORT_ADMISSION_CONSISTENT_RUNS", 3)
    require_certify: bool = _env_bool("BLOKPORT_ADMISSION_REQUIRE_CERTIFY", True)
    history_limit: int = 20   # rolling per-source run-event history kept in config.db

    def __post_init__(self):
        # fail loud on a nonsensical threshold (e.g. a hostile env override): a consistent_runs < 1 would
        # make ANY source eligible for auto with zero clean runs. No silent clamp -- surface the misconfig.
        if self.consistent_runs < 1:
            raise ValueError(f"BLOKPORT_ADMISSION_CONSISTENT_RUNS must be >= 1, got {self.consistent_runs}")
        if self.history_limit < 1:
            raise ValueError(f"admission history_limit must be >= 1, got {self.history_limit}")


@dataclass(frozen=True)
class BackendConstants:
    """Section 3.1 static values. Defaults observed in the real upload example
    (dev-staging environment). Per-source overrides live in sources.yaml."""

    # Category pcat ids derive from the CATEGORIES registry (single source of truth).
    @property
    def cat_slabs_pcat(self) -> str:
        return category("slab").pcat_id

    @property
    def cat_blocks_pcat(self) -> str:
        return category("block").pcat_id

    @property
    def cat_tiles_pcat(self) -> str:
        return category("tile").pcat_id

    # Owner ids, from env vars (settings top). sales_channel_id: one per env. company_id: the
    # general Blokport owner default; a per-scrape sources.yaml value overrides it (constants).
    sales_channel_id: str = SALES_CHANNEL_ID
    company_id: str = COMPANY_ID
    visibility: str = "public"
    discountable: str = "true"
    status: str = "published"
    # Variant defaults (section 3.1)
    variant_title: str = "Default"
    variant_manage_inventory: str = "true"
    variant_allow_backorder: str = "false"
    variant_option_1_name: str = "Default option"
    variant_option_1_value: str = "Default option value"


@dataclass(frozen=True)
class S3Config:
    # All env-driven so dev/prod differ by configuration only. The path segment
    # (dev/ vs prod/) follows BLOKPORT_ENV; bucket/region/profile/dry-run each
    # have their own override.
    bucket: str = S3_BUCKET
    region: str = S3_REGION
    # The S3 key prefix the image stage re-hosts product photos under. This is the
    # real staging location (blokport-dev-staging-3e58a6/dev/products/), and it MUST
    # match the path in ImagesConfig.public_base so the emitted URL resolves to the
    # uploaded object. Content-addressed: <prefix>/<src_site>/<sha256>.jpg.
    staging_prefix: str = f"{ENV_SEGMENT}/products/"
    # Empty by default -> boto3 default credential chain (the ECS task IAM role on
    # AWS; ambient creds locally). Set BLOKPORT_AWS_PROFILE only to force a named
    # local profile -- never on Fargate, where no profile exists (ProfileNotFound).
    credentials_profile: str = os.environ.get("BLOKPORT_AWS_PROFILE", "")
    # When true the image stage does not hit S3; it derives keys deterministically
    # and records them, but performs no network IO. Used when creds are absent.
    # Env-aware default: dev defaults dry (no accidental writes); PROD defaults to actually uploading, so a
    # prod deploy that forgets BLOKPORT_S3_DRY_RUN can't silently no-op every S3 write while emitting URLs
    # to objects that were never uploaded. Explicit BLOKPORT_S3_DRY_RUN still overrides in either env.
    dry_run: bool = _env_bool("BLOKPORT_S3_DRY_RUN", not IS_PRODUCTION)


@dataclass(frozen=True)
class ImageProcessingConfig:
    """Enhancement + de-watermark applied to scraped product photos during
    re-host (the local/s3 image modes; passthrough never downloads bytes so it
    cannot process them).

    These are photos of the ACTUAL slabs a customer buys, usually shot in a
    storage unit under poor, uneven light. Enhancement is Real-ESRGAN (learned
    clean + sharpen + 4x), then a gentle exposure lift + vibrance -- brighter and
    crisper while keeping the stone's true colour and vein pattern. It needs the
    torch stack (requirements-imageproc.txt); if that is unavailable the step is
    skipped with a warning (the image passes through un-enhanced -- no fallback).

    De-watermark runs ONLY on sources flagged `watermarked: true` in sources.yaml
    (e.g. varsha burns a visible mark into its photos): the logo crop is sent to FAL
    FLUX Fill (hosted) and composited back. Needs fal-client + FAL_KEY, not a local model.

    Disabled by default: until `enabled` is true the image stage behaves exactly
    as before (no processing, no new deps loaded). Enable per deployment with
    BLOKPORT_IMAGE_PROCESSING=true (the AWS image-processing container)."""

    enabled: bool = _env_bool("BLOKPORT_IMAGE_PROCESSING", False)
    # --- enhancement (Real-ESRGAN learned clean+sharpen+4x, then faithful beautify) ---
    esrgan_model: str = os.environ.get("BLOKPORT_ESRGAN_MODEL", "RealESRGAN_x4plus")
    esrgan_weights: str = os.environ.get("BLOKPORT_ESRGAN_WEIGHTS", "")  # path override; else models/<model>.pth
    esrgan_tile: int = 512            # per-tile input for large images (0 = whole image)
    target_long_edge: int = 2048      # cap the 4x output at this long edge (INTER_AREA downscale)
    levels_lo_pct: float = 0.5        # exposure lift: black-point percentile
    levels_hi_pct: float = 99.6       # exposure lift: white-point percentile
    vibrance: float = 0.20            # restore colour muted by bad light (0 = off)
    # --- de-watermark (flagged sources only; needs the GPU imageproc extras) ---
    # The mark is LOCATED by its (stone-absent) pink/magenta ink (_footprint), then only the logo
    # region is sent to FAL FLUX Fill (a hosted inpainter) to reconstruct it, and the result is
    # composited back through the logo mask so everything outside the logo is byte-identical. FAL is
    # billed per megapixel ($0.05, 1 MP floor, rounds up), so we send ONLY the padded logo crop of the
    # RAW image (before the ESRGAN upscale) -- that keeps every slab at the floor. Removing SDXL-inpaint
    # (which regenerated a whole square footprint and left a visible box on veined stone) also drops the
    # heavy diffusers stack. Runs in the GPU reprocess (needs network + FAL_KEY, not a local model).
    dewatermark: bool = True
    # De-watermark is an INSTRUCTION edit (FAL FLUX Kontext), not a masked inpaint: Kontext edits the whole
    # image from the prompt and reconstructs the stone under the logo without the mask-fill's hallucinations.
    fal_model: str = os.environ.get("BLOKPORT_FAL_MODEL", "fal-ai/flux-pro/kontext")
    # GENERIC fallback de-watermark instruction, used when a watermarked source has no per-source prompt
    # (SourceConfig.fal_prompt). It is source-AGNOSTIC on purpose -- it names no supplier -- so it is always
    # safe for any source (it removes whatever logo is present, never another supplier's specifically). Each
    # watermarked source SHOULD still set its own tuned prompt for best removal; this is the safe default.
    fal_prompt: str = ("Remove any supplier watermark logo and any overlaid brand text from the surface of "
                       "the stone slab, seamlessly reconstructing the natural stone pattern underneath. Keep "
                       "the stone colour, veining, the size/label card and everything else in the photo "
                       "identical to the original.")
    fal_seed: int = 0                 # fixed for best-effort reproducibility across re-runs
    fal_price_per_mp: float = 0.05    # USD per billed megapixel (rounds up, 1 MP floor)
    fal_max_usd: float = _env_float("BLOKPORT_FAL_MAX_USD", 150.0)  # per-run cost circuit breaker
    # Bounded attempts in the GPU reprocess before an image is TERMINALLY held. A completing image is
    # published (enhanced marker); one that still cannot complete after this many tries is written to the
    # discarded pool as a terminal HELD state, so every scraped image ends ready-or-held and the pipeline
    # always converges (ready + held == total) -- an unprocessable image never spins the indicator forever.
    enhance_max_attempts: int = 2
    crop_pad: int = 64                # context stone around the logo sent to FAL (px)
    crop_min_side: int = 512          # pad the crop up to this short side for fill quality (still < 1 MP)
    crop_snap: int = 16               # FLUX prefers dims divisible by 16
    # --- output / audit ---
    jpeg_quality: int = 85  # 85 is visually identical to 92 for photos at ~30-40% smaller files
    write_preview: bool = True        # images/reports/processed_preview.csv (source -> processed)
    # Improved and raw-scraped images are kept in two sibling folders under the
    # products prefix, so the upgraded set is cleanly separated from the originals:
    #   <products>/improved/<site>/<hash>.jpg   the enhanced image (Medusa uses this)
    #   <products>/scraped/<site>/<hash>.jpg    the raw download (audit / re-tuning)
    # Downloads are in-memory; the scraped copy is only persisted when keep_scraped
    # is on (the untouched original also still lives at the supplier URL). Handy in
    # dev while testing, optional in prod. The improved/ + scraped/ folder names are
    # part of the S3 layout and live in io.imagestore (single source of truth).
    keep_scraped: bool = _env_bool("BLOKPORT_KEEP_SCRAPED", False)
    # --- non-stone image discard (CLIP zero-shot; needs the same torch stack) ---
    # A scraped image is sometimes NOT a photo of the stone: a spec sheet, a price list, or a plain
    # vendor logo. CLIP scores each image against a "stone photo" prompt set vs a "document / logo" set;
    # an image the non-stone class wins with probability >= classify_margin is DISCARDED -- not enhanced,
    # not published -- and recorded in the discarded/ pool (io.imagestore). A variant whose EVERY image is
    # discarded emits the terminal no_publishable_image review flag (it publishes imageless, is not retried
    # as lost). Whole-image semantic score, NEVER text density: a slab photo WITH printed sizes, or a stone
    # photo carrying a small vendor logo, is still a stone photo and is KEPT. Reuses transformers (already a
    # dewatermark dep); the CLIP weights are baked into the imageproc/gpu images. Runs in the GPU reprocess
    # (deploy.reprocess_source); Stage 7 only reads the resulting pool, so :core stays torch-free.
    # Off by default (a discard is destructive): enable only once the margin is validated on a real sample.
    classify: bool = _env_bool("BLOKPORT_IMAGE_CLASSIFY", False)
    classify_model: str = os.environ.get("BLOKPORT_CLIP_MODEL", "openai/clip-vit-base-patch32")
    classify_margin: float = 0.85  # discard iff P(non-stone) >= this; conservative, keep when unsure
    classify_stone_prompts: tuple = (
        "a photograph of a natural stone slab",
        "a close-up photo of a polished stone surface",
        "a photo of marble or granite or quartzite texture",
        "a slab of stone in a warehouse",
    )
    classify_nonstone_prompts: tuple = (
        "a document or technical spec sheet",
        "a price list or printed catalogue page",
        "a plain company logo on a blank background",
        "a screenshot of text or a table",
    )


@dataclass(frozen=True)
class ImagesConfig:
    """Image staging (section 7 Stage 7). mode selects the storage backend:

      passthrough  use the source image URLs directly in the emit (no download).
                   The default until staging infrastructure exists.
      local        download bytes, content-address, store under local_staging_dir.
                   The interim 'staging bucket' on local disk.
      s3           download bytes, content-address, upload to the staging bucket.

    public_base is prepended to the content key for the emitted URL and is the
    same for local and s3, so the emitted URLs match once the local staging dir is
    synced to the bucket. Switching from local to s3 is a one-line mode change."""

    mode: str = os.environ.get("BLOKPORT_IMAGE_MODE", "passthrough")
    local_staging_dir: Path = REPO_ROOT / "state" / "image_staging"
    public_base: str = os.environ.get(
        "BLOKPORT_IMAGE_PUBLIC_BASE",
        f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{ENV_SEGMENT}/products/")
    concurrency: int = 8
    timeout: float = 20.0
    retries: int = 3
    require_images: bool = True  # a product with no image is rejected (Stage 9) -- we only list
    # products that have a picture, so 3_products_*.csv (and the inventory delta derived from the
    # same emitted set) never carry an imageless product.
    # HARD publish gate: link an image ONLY if the GPU actually enhanced it (an `enhanced/<sha>` marker
    # exists). Produce on the torch-free :core writes a RAW re-encode into improved/ without enhancing, so
    # improved/ presence alone is NOT proof of processing -- without this gate a raw, watermarked image can
    # reach the catalog. With it, an un-enhanced image is HELD (product publishes imageless) until the GPU
    # reprocess enhances it and writes the marker. Ships OFF: the markers must be backfilled for the already
    # enhanced set before flipping on, else every image holds. Enable with BLOKPORT_REQUIRE_ENHANCED=true.
    require_enhanced: bool = _env_bool("BLOKPORT_REQUIRE_ENHANCED", False)
    # Number of numbered "Product Image N" columns a slab/tile product can fill. The pipeline
    # fills as many as the product actually has, up to this cap; the rest stay blank. Shared by
    # the image-slotting stage and the emit columns so they can never drift (the template header
    # must carry exactly this many Product Image columns -- asserted in tests).
    product_image_slots: int = 15
    processing: ImageProcessingConfig = field(default_factory=ImageProcessingConfig)


@dataclass(frozen=True)
class MatchingConfig:
    """Advanced variation tier (section 5A.2 tier 8, semantic). Off by default; a heavy optional
    dependency that feeds review only. (Tier 7 / Splink retired -- the alias_resolver logistic model
    fills the tier-7 role.)"""

    enable_semantic: bool = False  # tier 8, embedding nearest-neighbour suggestion
    semantic_review_floor: float = 60.0  # below this a semantic hit is not even suggested
    semantic_model: str = "all-MiniLM-L6-v2"


@dataclass(frozen=True)
class CurationConfig:
    """Curation loop tunables (the new-variant / alias upload flow)."""

    # S3 staging base for variant catalog images (the small website image). Medusa
    # reads variant images from the dev/variations/ folder, so the variant Image is
    # {variant_image_base}{Key}.png. The bucket base is the ONLY env-specific part of
    # the catalog (dev vs prod): the {Key}.png image files are identical across
    # environments, so the SAME generated images upload to both buckets. Build the
    # prod variant file by setting BLOKPORT_VARIANT_IMAGE_BASE to the prod bucket and
    # re-running the catalog (stages/emit_catalog.py stamps it).
    # derive from the env's bucket + segment (dev/ vs prod/) so a prod build NEVER stamps the
    # dev path; the BLOKPORT_VARIANT_IMAGE_BASE override is for an out-of-band bucket only.
    variant_image_base: str = os.environ.get(
        "BLOKPORT_VARIANT_IMAGE_BASE",
        f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{ENV_SEGMENT}/variations/")
    # Base images per category derive from the CATEGORIES registry (image_prompts
    # selects by Key prefix; these accessors remain for any direct callers).
    @property
    def variant_base_image_slab(self) -> str:
        return category("slab").base_image

    @property
    def variant_base_image_block(self) -> str:
        return category("block").base_image

    @property
    def variant_base_image_tile(self) -> str:
        return category("tile").base_image

    # (Volume per kg (m³/kg) is per-category now; see CATEGORIES[*].volume_per_kg.)
    # a gap whose nearest existing variety scores at or above this is proposed as
    # an ALIAS of that variety rather than a new variant, to avoid creating
    # near-duplicate variants for what suppliers just renamed.
    alias_suggest_floor: float = 70.0
    # Tier-7 alias model (matching.alias_resolver): a trained entity-resolution model replaces the
    # flat alias_suggest_floor for the alias-vs-new decision. P>=hi -> confirmed alias, P<=lo ->
    # mint new, the uncertain middle -> review. The hi/lo gap is the automation dial: wider = less
    # review, more risk. Falls back to alias_suggest_floor when the model can't train.
    enable_alias_model: bool = True
    alias_model_hi: float = 0.90
    alias_model_lo: float = 0.20


# --- Category registry: the SINGLE source of truth for categories --------------
@dataclass(frozen=True)
class Category:
    """One product category. Adding a category = ONE entry here (plus its data
    files); every stage derives the category set from this list, so a misspelled
    or unwired category fails loudly instead of silently becoming a slab.

    The behaviour flags let a category opt out of the stone-variety model: slab/
    block/tile are FORMS of one stone variety (shared vocabulary, fan-out, texture
    -swap images), whereas e.g. accessories would set shares_variety_vocab=False,
    fan_out=False, base_image="" (own vocabulary, own backbone, real photos)."""

    name: str                   # canonical lowercase; also the variant Key prefix
    plural: str                 # inbox folder + tree-build group key
    label: str                  # Medusa category name / backbone "category" value
    pcat_id: str                # Medusa product-category id ("" until created)
    backbone_filename: str      # under catalog_source/
    base_image: str             # fal.ai base for texture generation ("" = real photo)
    shares_variety_vocab: bool  # shares the stone-variety vocabulary (slab/block/tile)
    fan_out: bool               # a new variety is also created in this category
    mirror_of: str | None       # backbone mirrors this category's (tile -> slab) else None
    volume_per_kg: str = ""     # static "Volume per kg (m³/kg)" written into every variant
    pcat_env_var: str | None = None  # env override for pcat_id (bootstrap); kept so a post-fetch refresh
                                     # re-derives pcat_id the SAME way construction did (see refresh_category_pcats)
    bulk_form: bool = False     # the uncut/solid form (block for stone): drives row.is_block + the block-scale
                                # dimension/freight branch. A domain with no such form leaves this unset everywhere.
    default_form: bool = False  # the form a row falls back to when its format is unresolved; also the finish and
                                # dimension fallback source. Exactly one category declares it (pack-validated).

    @property
    def active(self) -> bool:
        """Live once its Medusa category id exists."""
        return bool(self.pcat_id)

    @property
    def backbone_path(self) -> Path:
        return WORKSPACE_ROOT / "catalog_source" / self.backbone_filename


# DEV/PROD SEGREGATION: a category's Medusa pcat id is INSTANCE-SPECIFIC (the dev and prod
# Medusa assign different ids), so it must come from the env's own download, never a hardcoded
# value. Read it from from_medusa/<env>/attributes.csv (the same file the reference loads) so a
# prod run uses prod pcats and a dev run uses dev pcats -- the ids can never cross. The literals
# below are only a bootstrap fallback for an env whose attributes.csv isn't downloaded yet.
def _env_category_pcats() -> dict[str, str]:
    path = _FROM_MEDUSA / "attributes.csv"
    out: dict[str, str] = {}
    if path.exists():
        import csv as _csv
        with path.open(encoding="utf-8-sig", newline="") as h:
            for r in _csv.DictReader(h):
                if (r.get("category") or "").strip() == "category" and (r.get("sourceid") or "").strip():
                    out[(r.get("value") or "").strip()] = r["sourceid"].strip()
    return out


_ENV_PCATS = _env_category_pcats()  # {'Slabs': pcat, 'Blocks': pcat, 'Tiles': pcat} for THIS env


def _pcat(label: str, env_var: str | None = None) -> str:
    """A category's Medusa pcat for THIS env, sourced ONLY from the env's Medusa export
    (from_medusa/<env>/attributes.csv) -- never hardcoded. An explicit env-var override is allowed.
    A category absent from the export stays "" (inactive, emits nothing), so dev and prod ids can
    never cross and no stale literal can leak."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return _ENV_PCATS.get(label, "")

# The category list is declared by the active product-domain pack (config/domains/<pack>.yaml). Adding a
# category = ADD AN ENTRY THERE; every category-derived constant in the pipeline recomputes from this tuple
# at import. The Medusa pcat id stays env-derived (_pcat, from the env's attributes.csv), never in the pack,
# so dev/prod ids never cross. `_BY_NAME` below is the runtime index the helpers read (activation via the
# env pcat, and tests, can flip a category). The stone pack reproduces the historical slab/block/tile tuple
# exactly. Full recipe: CATEGORY_GUIDE.md.
def _build_categories() -> "tuple[Category, ...]":
    return tuple(
        Category(
            c["name"], c["plural"], c["label"],
            _pcat(c["label"], c.get("pcat_env_var")), c["backbone_filename"], c["base_image"],
            shares_variety_vocab=c["shares_variety_vocab"], fan_out=c["fan_out"],
            mirror_of=c.get("mirror_of"), volume_per_kg=c.get("volume_per_kg", ""),
            pcat_env_var=c.get("pcat_env_var"),
            bulk_form=c.get("bulk_form", False), default_form=c.get("default_form", False))
        for c in active_pack().categories
    )


CATEGORIES: tuple[Category, ...] = _build_categories()

_BY_NAME = {c.name: c for c in CATEGORIES}
_BY_LABEL = {c.label: c for c in CATEGORIES}
_BY_LABEL_CF = {c.label.casefold(): c for c in CATEGORIES}  # plural label, casefolded


def category(name: str) -> "Category | None":
    # accept the singular name ('slab') OR the plural label ('Slabs') so a scraper that tags
    # its format in the plural ('Blocks'/'Tiles') still resolves the explicit tag instead of
    # silently falling through to the slab default.
    key = (name or "").strip().casefold()
    return _BY_NAME.get(key) or _BY_LABEL_CF.get(key)


def category_by_label(label: str) -> "Category | None":
    return _BY_LABEL.get((label or "").strip())


def category_for_key(key: str) -> "Category | None":
    """The category a variant Key belongs to, by its prefix (slab_..., tile_...)."""
    return _BY_NAME.get((key or "").split("_", 1)[0].casefold())


def default_form_name() -> str:
    """The category a row falls back to when its format is unresolved (also the finish/dimension fallback
    source). Declared by the pack's `default_form: true`; fail loud if the pack declares none (a pack that
    passes _validate_shape always has exactly one, so this raise is only a defence against a bypassed load)."""
    for c in _BY_NAME.values():
        if c.default_form:
            return c.name
    raise RuntimeError("active domain pack declares no default_form category")


def bulk_form_name() -> "str | None":
    """The uncut/solid form's category name (block for stone), or None when the domain has no such form.
    Drives row.is_block and the block-scale dimension/freight branch."""
    for c in _BY_NAME.values():
        if c.bulk_form:
            return c.name
    return None


def active_categories() -> tuple[Category, ...]:
    # iterate _BY_NAME (the runtime registry) so tests can flip a category's pcat
    return tuple(c for c in _BY_NAME.values() if c.active)


def refresh_category_pcats() -> None:
    """Re-read the category pcats from the from_medusa/<env>/attributes.csv now on disk and rebuild the
    runtime registry (_BY_NAME et al., which the helpers read).

    _ENV_PCATS is read ONCE at import, as a bootstrap from whatever export is on disk then. But a produce
    downloads a FRESH attributes.csv AFTER import (produce._fetch_inputs), so a running task whose local
    export changed meaning -- a new Medusa pcat id, or a corrected category label -- would otherwise keep
    gating on the stale import snapshot: every row rejected category_invalid (validate) and new-variety
    fan-out silently disabled (curate), both off the frozen active_categories(). Call this once the fresh
    export is on disk so the registry reflects the live pcats. Each category's pcat_id is re-derived the
    SAME way construction did (_pcat honours its env override), so the refresh is a faithful recompute,
    not a second code path. Idempotent: a no-op when the export is unchanged."""
    global _ENV_PCATS, _BY_NAME, _BY_LABEL, _BY_LABEL_CF
    _ENV_PCATS = _env_category_pcats()
    rebuilt = tuple(replace(c, pcat_id=_pcat(c.label, c.pcat_env_var)) for c in _BY_NAME.values())
    _BY_NAME = {c.name: c for c in rebuilt}
    _BY_LABEL = {c.label: c for c in rebuilt}
    _BY_LABEL_CF = {c.label.casefold(): c for c in rebuilt}


@dataclass(frozen=True)
class AutoEnhanceConfig:
    """After a produce stages new raw images, auto-submit the GPU reprocess for just the new ones, so
    enhancement/de-watermark happens without a manual step (the Batch compute env spins a GPU up on demand
    and back to zero when idle). Fire-and-forget: produce submits and returns; the enhanced images land in
    improved/ and the NEXT produce links them (the existing one-cycle hold). See deploy/enhance_trigger.

    Off by default (BLOKPORT_AUTO_ENHANCE): ships dark, enabled per deployment. queue/job_definition name
    the Batch target (from the gpu_enhance module, injected as env on the produce task). slice_size caps a
    single job's image count so a big cold-start fans out across GPUs. The submitted reprocess always runs
    CLASSIFY=false -- auto-enhance never auto-discards (that needs a calibrated margin, enabled separately)."""

    enabled: bool = _env_bool("BLOKPORT_AUTO_ENHANCE", False)
    queue: str = os.environ.get("BLOKPORT_GPU_QUEUE", "")
    job_definition: str = os.environ.get("BLOKPORT_GPU_JOBDEF", "")
    slice_size: int = _env_int("BLOKPORT_ENHANCE_SLICE", 200)
    # Global fan-out cap: at most this many Batch jobs per trigger. Each job carries its OWN per-process
    # fal_max_usd ceiling, so without a cap a large backlog fans out one job per window (unbounded jobs) and
    # multiplies the FAL ceiling N-fold. With the cap, the worst-case FAL exposure per trigger is bounded at
    # max_jobs * fal_max_usd; deferred windows stay un-done and ride the next trigger. <=0 disables the cap.
    max_jobs: int = _env_int("BLOKPORT_ENHANCE_MAX_JOBS", 8)


@dataclass(frozen=True)
class AutoTextureConfig:
    """After a produce queues NEW-variant textures the lean :core cannot generate (no fal_client/torch),
    auto-submit ONE on-demand GPU Batch job to generate + upload them (deploy/texture_trigger). Part of the
    produce (Republish), NOT a cron: it fires only when a produce actually queued textures. The job reuses
    the SAME GPU queue/job-definition as auto_enhance (the :gpu image runs both, selected by RUN_MODE=
    generate-textures -> prepare_variant_images, the unchanged FLUX.2 -> BEN2 -> upload pipeline); a separate
    flag so textures and photo-enhance enable independently. Fire-and-forget; the next produce stamps the
    images (the same one-cycle hold as auto_enhance). Off by default (BLOKPORT_AUTO_TEXTURE)."""

    enabled: bool = _env_bool("BLOKPORT_AUTO_TEXTURE", False)
    queue: str = os.environ.get("BLOKPORT_GPU_QUEUE", "")
    job_definition: str = os.environ.get("BLOKPORT_GPU_JOBDEF", "")


@dataclass(frozen=True)
class Settings:
    environment: str = BLOKPORT_ENV  # "development" | "production" (BLOKPORT_ENV)
    # A hash of the live id set (section 3.2). Computed from reference data at
    # M1; a mismatch against the live backend aborts. Empty means "not pinned yet".
    backend_id_fingerprint: str = ""
    code_version: str = "0.1.0"

    paths: Paths = field(default_factory=Paths)
    thresholds: Thresholds = field(default_factory=Thresholds)
    admission: Admission = field(default_factory=Admission)
    backend: BackendConstants = field(default_factory=BackendConstants)
    s3: S3Config = field(default_factory=S3Config)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    auto_enhance: AutoEnhanceConfig = field(default_factory=AutoEnhanceConfig)
    auto_texture: AutoTextureConfig = field(default_factory=AutoTextureConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    curation: CurationConfig = field(default_factory=CurationConfig)

    # The source proven first (section 14). develi is absent from the supplied
    # data, so polonine is the clean named-variety proving source.
    spine_source: str = "polonine"


SETTINGS = Settings()
