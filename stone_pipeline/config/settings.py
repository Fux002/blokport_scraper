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
ENV_SEGMENT = "prod" if IS_PRODUCTION else "dev"      # S3 path/key namespace (dev/... vs prod/...)
ENV_NAME = "production" if IS_PRODUCTION else "development"  # workspace folder per env

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


# Owner Medusa ids — operational config, NOT catalog attributes, so they are environment
# variables (the catalog attribute ids — color/finish/type/quality/category — come from the
# env's attributes.csv instead). The sales channel is ONE id per environment; the company is
# the general Blokport owner by default and can be overridden per scrape in sources.yaml. Each
# has a dev default for local runs; a PRODUCTION run must set the env var — it never falls back
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
    country_codes_csv: Path = REPO_ROOT / "reference" / "country_codes.csv"
    placeholder_hashes_csv: Path = REPO_ROOT / "reference" / "placeholder_hashes.csv"
    standard_slab_area_csv: Path = REPO_ROOT / "reference" / "standard_slab_area.csv"

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
    health_fill_drop_warn: float = 0.15
    health_fill_drop_fail: float = 0.40
    health_rowcount_floor: float = 0.50


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
    # local profile — never on Fargate, where no profile exists (ProfileNotFound).
    credentials_profile: str = os.environ.get("BLOKPORT_AWS_PROFILE", "")
    # When true the image stage does not hit S3; it derives keys deterministically
    # and records them, but performs no network IO. Used when creds are absent.
    # Default true in dev; set BLOKPORT_S3_DRY_RUN=false to actually upload.
    dry_run: bool = _env_bool("BLOKPORT_S3_DRY_RUN", True)


@dataclass(frozen=True)
class ImageProcessingConfig:
    """Faithful enhancement + de-watermark applied to scraped product photos
    during re-host (the local/s3 image modes; passthrough never downloads bytes
    so it cannot process them).

    These are photos of the ACTUAL slabs a customer buys, usually shot in a
    storage unit under poor, uneven light. The goal is to fix that — exposure,
    white balance, local contrast, noise, softness — WITHOUT inventing detail
    (no generative super-resolution): the picture must stay a true record of the
    stone. Pixel upscaling uses high-quality Lanczos resampling, not a model.

    De-watermark runs ONLY on sources flagged `watermarked: true` in sources.yaml
    (e.g. varsha burns a visible mark into its photos). It needs the optional
    torch stack (requirements-imageproc.txt); if those deps are absent the step
    is skipped with a warning and enhancement still runs.

    Disabled by default: until `enabled` is true the image stage behaves exactly
    as before (no processing, no new deps loaded). Enable per deployment with
    BLOKPORT_IMAGE_PROCESSING=true (the AWS image-processing container)."""

    enabled: bool = _env_bool("BLOKPORT_IMAGE_PROCESSING", False)
    # --- enhancement engine ---------------------------------------------------
    # "esrgan"   : Real-ESRGAN learned clean+sharpen+4x upscale, then a gentle exposure lift +
    #              vibrance. Faithful (colour untouched, natural texture); needs the GPU extras
    #              (torch + spandrel + the pinned weights). This is the intended production engine.
    # "classical": the legacy OpenCV chain below (fast on CPU but distorts colour/texture). Used
    #              as a graceful fallback when the ESRGAN model/torch is unavailable.
    engine: str = os.environ.get("BLOKPORT_IMAGE_ENGINE", "esrgan").strip().lower()
    esrgan_model: str = os.environ.get("BLOKPORT_ESRGAN_MODEL", "RealESRGAN_x4plus")
    esrgan_weights: str = os.environ.get("BLOKPORT_ESRGAN_WEIGHTS", "")  # path override; else models/<model>.pth
    esrgan_tile: int = 512            # per-tile input for large images (0 = whole image)
    target_long_edge: int = 2048      # cap the 4x output at this long edge (INTER_AREA downscale)
    levels_lo_pct: float = 0.5        # exposure lift: black-point percentile
    levels_hi_pct: float = 99.6       # exposure lift: white-point percentile
    vibrance: float = 0.20            # restore colour muted by bad light (0 = off)
    # --- faithful enhancement (classical engine) ---
    enhance: bool = True              # white balance + CLAHE local contrast + exposure
    denoise: bool = True              # gentle chroma/luma denoise
    denoise_strength: int = 3         # cv2 fastNlMeans h; keep low to avoid smearing veins
    clahe_clip: float = 2.0           # local-contrast strength; higher = punchier, riskier
    sharpen_amount: float = 0.6       # unsharp-mask amount (0 = off)
    # --- pixel upscaling (faithful, Lanczos — never generative) ---
    upscale: bool = True
    upscale_max_scale: float = 2.0    # never enlarge beyond this factor
    upscale_target_long_edge: int = 1600  # stop upscaling once the long edge reaches this
                                          # (1600 is crisp for web + retina; 2048 only helps deep zoom and ~2x the bytes)
    # --- de-watermark (flagged sources only; needs the GPU imageproc extras) ---
    # The mark is located by its (stone-absent) pink ink + text strokes, then its small central
    # footprint is REGENERATED with a learned inpainting model (SDXL-inpaint) — natural matching
    # stone texture, not the smear a classical fill leaves on patterned slabs. The exact pixels
    # under a drifting, multi-position semi-transparent mark can't be recovered, so this small
    # region is reconstructed; everything else is untouched.
    dewatermark: bool = True
    dewatermark_model: str = os.environ.get(
        "BLOKPORT_DEWATERMARK_MODEL", "diffusers/stable-diffusion-xl-1.0-inpainting-0.1")
    dewatermark_steps: int = 25
    dewatermark_guidance: float = 7.5
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
    (from_medusa/<env>/attributes.csv) — never hardcoded. An explicit env-var override is allowed.
    A category absent from the export stays "" (inactive, emits nothing), so dev and prod ids can
    never cross and no stale literal can leak."""
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    return _ENV_PCATS.get(label, "")

# The category list. Adding a category = ADD AN ENTRY HERE (a source edit): every
# category-derived constant in the pipeline recomputes from this tuple at import.
# `_BY_NAME` below is the runtime index the helpers read (so activation via the env
# pcat, and tests, can flip a category); behaviour is keyed off the flags + active.
# Full recipe: CATEGORY_GUIDE.md.
CATEGORIES: tuple[Category, ...] = (
    Category("slab", "slabs", "Slabs",
             _pcat("Slabs"), "backbone_slabs.json",
             "https://v3b.fal.media/files/b/0a9ef2cf/Mfbt9fWxn_4taQ9Pe-wvs_base_slab_3.jpg",
             shares_variety_vocab=True, fan_out=True, mirror_of=None, volume_per_kg="0.0017"),
    Category("block", "blocks", "Blocks",
             _pcat("Blocks"), "backbone_blocks.json",
             "https://v3b.fal.media/files/b/0a9f301e/Vlbo_xk9vDJtYKhEbkxBZ_base_block.jpeg",
             shares_variety_vocab=True, fan_out=True, mirror_of=None, volume_per_kg="0.0014348"),
    # tiles (and other non-slab categories) use the block volume per kg
    Category("tile", "tiles", "Tiles",
             _pcat("Tiles", "BLOKPORT_CAT_TILES_PCAT"),
             "backbone_tiles.json",
             "https://v3b.fal.media/files/b/0a9f30f3/jdmNyxqelKJM4DxFi4jyG_base_tiles.png",
             shares_variety_vocab=True, fan_out=True, mirror_of="slab", volume_per_kg="0.0014348",
             pcat_env_var="BLOKPORT_CAT_TILES_PCAT"),
)

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
class Settings:
    environment: str = BLOKPORT_ENV  # "development" | "production" (BLOKPORT_ENV)
    # A hash of the live id set (section 3.2). Computed from reference data at
    # M1; a mismatch against the live backend aborts. Empty means "not pinned yet".
    backend_id_fingerprint: str = ""
    code_version: str = "0.1.0"

    paths: Paths = field(default_factory=Paths)
    thresholds: Thresholds = field(default_factory=Thresholds)
    backend: BackendConstants = field(default_factory=BackendConstants)
    s3: S3Config = field(default_factory=S3Config)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    curation: CurationConfig = field(default_factory=CurationConfig)

    # The source proven first (section 14). develi is absent from the supplied
    # data, so polonine is the clean named-variety proving source.
    spine_source: str = "polonine"


SETTINGS = Settings()
