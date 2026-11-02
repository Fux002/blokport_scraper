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

from dataclasses import dataclass, field, replace
from enum import IntEnum
from pathlib import Path

from stone_pipeline.config.domain import active_pack  # dependency-free (yaml only); no import cycle
from stone_pipeline.core import env  # neutral SCRAPER_ env prefix (BLOKPORT_ fallback); no import cycle

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
#
# BLOKPORT_ENV IS THE DEPLOYMENT TIER, AND NOTHING ELSE. It is not a tenant, brand or material slot --
# BRAND (below) and BLOKPORT_DOMAIN_PACK own those. Every production guard in this file keys off
# IS_PRODUCTION, so an unrecognised value that merely FAILED to look like production would silently
# downgrade a prod deployment to dev semantics: dev S3 prefix, S3_DRY_RUN defaulting true, and the
# bucket / brand / sales-channel guards below disabled -- a run that looks healthy while writing
# nothing real. A brand-prefixed value such as "wudport-production" is the obvious way to trip it.
#
# So the value is VALIDATED against a closed set and anything else raises HERE, at import, before a
# single guard can be silently skipped.
_ENV_ALIASES = {
    "development": "development", "dev": "development",
    "production": "production", "prod": "production",
}
_RAW_ENV = (env.getenv("BLOKPORT_ENV", "development") or "").strip().lower()
if _RAW_ENV not in _ENV_ALIASES:
    raise RuntimeError(
        f"SCRAPER_ENV/BLOKPORT_ENV={_RAW_ENV!r} is not a valid deployment tier. "
        f"Allowed: {', '.join(sorted(_ENV_ALIASES))}. It selects the TIER only -- it is not a "
        "brand/tenant/material slot, and an unrecognised value would silently run with DEVELOPMENT "
        "semantics (dev S3 prefix, dry-run on, bucket + brand + sales-channel guards off). Set the "
        "brand with BRAND and the material with BLOKPORT_DOMAIN_PACK; give each its OWN deployment "
        "(own S3 bucket, own task, own config.db + ledger). See DOMAIN_PACK.md.")
# Canonical, so downstream sees "development"/"production" even when the alias "dev"/"prod" was set.
BLOKPORT_ENV = _ENV_ALIASES[_RAW_ENV]
IS_PRODUCTION = BLOKPORT_ENV == "production"
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
    """Read a boolean env var (1/true/yes/on); fall back to default when unset. Delegates to the one
    boolean-env parser in core.env so the truthy vocabulary lives in a single place."""
    return env.env_bool(name, default)


def _env_int(name: str, default: int) -> int:
    """Read an integer env var: the default ONLY when unset. A set-but-malformed override fails loud at
    config import, naming the variable: an operator who typed FAL_MAX_USD=abc meant a value, and silently
    running on the default is the no_image incident class (a run proceeding on a value nobody set)."""
    raw = env.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def _env_float(name: str, default: float | None) -> float | None:
    """Read a float env var: the default ONLY when unset; a malformed override fails loud (see _env_int)."""
    raw = env.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


# Brand identity (multi-brand). Each brand (blokport/wudport/calcport/...) is its OWN deployment: own
# bucket, own Medusa, own domain pack. Dev defaults to blokport (single-brand by design). BRAND is
# brand-neutral (env BRAND; legacy BLOKPORT_BRAND read as fallback). Defined BEFORE the bucket so the
# non-blokport guard below can reference it.
BRAND = (env.getenv("BRAND") or "").strip().lower() or ("blokport" if not IS_PRODUCTION else "")
if IS_PRODUCTION and not BRAND:
    raise RuntimeError(
        "BRAND must be set in production (the brand this deployment serves, e.g. 'blokport', 'wudport', "
        "'calcport') so the brand<->bucket guard can refuse a cross-brand mis-configuration.")


# ONE predicate for "the hardcoded blokport dev defaults may apply": blokport is the owning brand, so its
# dev bucket / owner ids are the only baked-in defaults, and ONLY for blokport-in-dev. Every other case
# (production, or any non-blokport brand even in dev) must supply its own -- never inherit blokport's. This
# is the single source of truth for that rule so the brand-isolation guarantee can't drift across the guards
# below (previously the `BRAND == "blokport"` literal was repeated at each site).
def _is_blokport_dev() -> bool:
    return not IS_PRODUCTION and BRAND == "blokport"

# The S3 bucket. The blokport DEV bucket is the only known default; a PROD run OR any non-blokport brand
# (even in dev) MUST set SCRAPER_S3_BUCKET explicitly -- never silently write into blokport's bucket.
_DEV_S3_BUCKET = "blokport-dev-staging-3e58a6"
_S3_BUCKET_ENV = (env.getenv("BLOKPORT_S3_BUCKET", "") or "").strip()
if not _S3_BUCKET_ENV and not _is_blokport_dev():
    raise RuntimeError(
        "SCRAPER_S3_BUCKET must be set for this deployment (any production run, or any non-blokport brand "
        f"even in dev) -- refusing to default to the blokport dev bucket ({_DEV_S3_BUCKET}). Set "
        "SCRAPER_S3_BUCKET to this brand's own bucket.")
S3_BUCKET = _S3_BUCKET_ENV or _DEV_S3_BUCKET
S3_REGION = env.getenv("BLOKPORT_S3_REGION", "eu-west-1")

# The config runner's ceiling on one run: it kills a run past it as wedged (Terraform sets 4 h on the service). The
# photo process fits its work window inside it and treats a run lock not refreshed for this long as a killed run's.
RUN_TIMEOUT_SECONDS = _env_int("SCRAPER_RUN_TIMEOUT_SECONDS", 7200)
# On SIGTERM (ECS stops the task) the config server ends the running run within this one deadline: the child's grace to
# clean up after its SIGTERM, a kill and the clearing of what a killed photo run left, and its watcher finishing the run
# record. It leaves 40 s of the config container's stopTimeout (120 s, the Fargate maximum) for the last config save.
# The same deadline bounds a photos run giving way to another stage.
RUN_STOP_GRACE_SECONDS = _env_int("SCRAPER_RUN_STOP_GRACE_SECONDS", 80)

# Brand<->bucket cross-check: the bucket must BELONG to this brand, so a bucket mis-set to ANOTHER brand's
# store fails loud instead of crossing brands (and, since it runs before any snapshot restore, it also stops
# a wrong-brand ledger/config.db from ever being pulled). Prefix, not substring: 'blokport' must not match
# 'notblokport-x' or a bucket that merely contains the slug. Fires for any prod run OR any non-blokport brand
# even in dev -- the same scope as the require-guard above. Convention: '<brand>-<env>-staging[...]'.
def _bucket_matches_brand(brand: str, bucket: str) -> bool:
    return bool(brand) and bucket.lower().startswith(f"{brand}-")


if not _is_blokport_dev() and not _bucket_matches_brand(BRAND, S3_BUCKET):
    raise RuntimeError(
        f"SCRAPER_BRAND={BRAND!r} but the bucket {S3_BUCKET!r} is not this brand's (expected prefix "
        f"'{BRAND}-') -- refusing to run. A bucket set to ANOTHER brand's store would cross brands. Set "
        f"SCRAPER_S3_BUCKET to {BRAND}'s own bucket.")


# Owner Medusa ids -- operational config, NOT catalog attributes (those come from the env's attributes.csv).
# One sales channel + one company per deployment. In PRODUCTION they MUST be set: an empty owner id would
# stamp every product with a blank/wrong Medusa owner, so we FAIL LOUD rather than returning "". The dev
# defaults below are BLOKPORT's -- applied only for the blokport brand in dev; a non-blokport dev must set
# its own (else empty, never blokport's).
_DEV_SALES_CHANNEL_ID = "sc_01KTM2B2DJNSW6WPS1Q8FN8B2R"
_DEV_COMPANY_ID = "01KTV98X8RG743YR3QHCECZKKA"


def _owner_id(env_var: str, dev_default: str, label: str, *, required_in_prod: bool) -> str:
    val = (env.getenv(env_var, "") or "").strip()
    if val:
        return val
    if IS_PRODUCTION and required_in_prod:
        raise RuntimeError(
            f"{env.PREFIX + env._suffix(env_var)} (the {label}) must be set in production -- refusing to run "
            "with an empty owner id that would mis-own or blank every product/variation in Medusa. Set it "
            "for this brand.")
    # dev: the hardcoded defaults are BLOKPORT's -- apply them only for the blokport brand, never another's.
    return dev_default if _is_blokport_dev() else ""


# sales channel is ONE id per deployment with NO fallback, so it is REQUIRED in prod (an empty channel is a
# broken storefront). company is the general owner DEFAULT and is overridable per-source (Medusa resolves an
# empty one by vendor name), so it may be empty -- NOT required in prod.
SALES_CHANNEL_ID = _owner_id("BLOKPORT_SALES_CHANNEL_ID", _DEV_SALES_CHANNEL_ID, "sales channel id",
                             required_in_prod=True)
COMPANY_ID = _owner_id("BLOKPORT_COMPANY_ID", _DEV_COMPANY_ID, "company id", required_in_prod=False)


# Hand-maintained catalog_source/ files (backbones, origin_map, supplier-overrides) are MATERIAL-specific.
# The stone pack (historical) keeps them flat at catalog_source/<file>; any OTHER pack MUST ship its own
# catalog_source/<pack>/<file> and NEVER falls back to stone's -- so a wood/lime run reads its own data or
# gaps/fails loudly, never silently loads stone varieties/origins (audit A2 / #213 M1).
_STONE_PACK = "stone"


def _catalog_source_file(filename: str) -> Path:
    base = WORKSPACE_ROOT / "catalog_source"
    pack = active_pack().name
    return base / filename if pack == _STONE_PACK else base / pack / filename

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
    def backbone_json(self) -> Path:        # the DEFAULT-FORM backbone (the pack's primary category), NOT
        # literally slab -- a non-stone pack whose default form is not named 'slab' would otherwise crash here
        # (category('slab') -> None -> AttributeError) on the arg-less load_backbone hot path.
        return category(default_form_name()).backbone_path

    # A specific category's backbone is category(<name>).backbone_path -- no per-product-type accessor.

    @property
    def type_density_csv(self) -> Path:
        # per-material density, PACK-NAMESPACED (reference/type_density.<pack>.csv) so each material's
        # densities are separate. A pack with no such file uses its own default_density (domain pack), never
        # the stone file -- so wood never inherits stone's 2700. Seeded from that env's Medusa type table.
        return REPO_ROOT / "reference" / f"type_density.{active_pack().name}.csv"

    @property
    def standard_slab_area_csv(self) -> Path:
        # PACK-NAMESPACED like type_density; absent for a pack with no standard-area concept (the bundle-area
        # ladder then simply falls through), so it never reads stone's slab-area table under another pack.
        return REPO_ROOT / "reference" / f"standard_slab_area.{active_pack().name}.csv"

    @property
    def origin_map_csv(self) -> Path:
        # per-variety origin, PACK-NAMESPACED via catalog_source/<pack>/ (stone stays flat). A non-stone pack
        # reads its OWN origin_map or none -- never stone's, so wood/lime origins can't silently resolve to a
        # stone variety's country.
        return _catalog_source_file("origin_map.csv")

    @property
    def origin_overrides_csv(self) -> Path:
        # per-supplier origin overrides, same pack-namespacing. Optional (absent file = no overrides).
        return _catalog_source_file("origin_supplier_overrides.csv")
    # ports.csv carries Medusa PORT IDS, which differ per environment (exactly like attributes.csv), so
    # it lives under from_medusa/<env>/ and is fetched from that env's Medusa. The fallback is the
    # committed catalog_source master (the 601-port name/LOCODE list, carrying DEV ids). load_ports
    # REFUSES this dev fallback in production -- prod resolves only against its own downloaded file.
    ports_csv: Path = _FROM_MEDUSA / "ports.csv"
    ports_csv_fallback: Path = WORKSPACE_ROOT / "catalog_source" / "ports.csv"
    units_csv: Path = REPO_ROOT / "reference" / "units.csv"
    # origin_map + origin_overrides are HAND-MAINTAINED material-specific catalog_source files -> now
    # PACK-NAMESPACED properties (below), so a non-stone pack reads its own or gaps loudly, never stone's.
    country_codes_csv: Path = REPO_ROOT / "reference" / "country_codes.csv"
    placeholder_hashes_csv: Path = REPO_ROOT / "reference" / "placeholder_hashes.csv"
    # type_density_csv + standard_slab_area_csv are PACK-NAMESPACED properties (below), not shared files, so a
    # non-stone pack never inherits stone's per-type densities / slab-area table.

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
    # matching tiers: the character-similarity floor under the phonetic tier (so a metaphone hit never
    # merges names that barely share letters); the leaf snap floor (a chosen attribute value snaps onto the
    # nearest allowed one at or above this fuzzy score, flagged)
    phonetic_char_floor: float = 85.0
    leaf_snap_floor: float = 80.0
    # format resolution from a stated depth: at or above bulk_depth_fraction x the bulk form's depth floor
    # it is the bulk form; at or below default_depth_multiple x the default form's thickness top it is the
    # default form; the middle is ambiguous and declines
    bulk_depth_fraction: float = 0.3
    default_depth_multiple: float = 3.0
    # magnitude drift (stages/magnitude_drift): a per-format median moving by warn x is a warning, by fail x
    # aborts before emit (a unit/normalisation slip); a format needs min_sample rows for a stable median
    magnitude_warn_factor: float = 4.0
    magnitude_fail_factor: float = 10.0
    magnitude_min_sample: int = 8
    # run gates: an adapter that drops more than this share of rows at adapt is a mis-mapped required field
    # (abort); a single run that would discontinue more than this share of a source's known products is a
    # partial scrape (delist refused)
    adapt_drop_abort_fraction: float = 0.5
    delist_max_fraction: float = 0.30
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
    # rows a stage dead-lettered on an unexpected exception (stages/_rowguard.py). A lone bad-data row stays
    # isolated (surfaced per-row, never escalated); a systemic share tripping this points at a code bug and
    # goes DEGRADED so it is loud. Low, because ANY appreciable isolated fraction is abnormal.
    row_isolated_degraded: float = 0.02
    # ABORT floor for the same signal: when this large a share of rows was dead-lettered, it is a systemic
    # code bug (e.g. one input class every row hits), not sparse bad data -- fail the source LOUD (like the
    # >50% adapt-drop / ingest / magnitude gates) rather than report it a clean success that shipped almost
    # nothing. Below this, per-row isolation does its job (ship the good rows, flag the few dead ones).
    row_isolated_abort: float = 0.25
    # A1: when a scrape reports that more than this fraction of its per-product DETAIL fetches failed
    # (rate-limited), its product absences are untrustworthy -- refuse to delist against it. Below the 30%
    # delist cap so a moderately rate-limited run (10-25% detail failures) that would otherwise silently
    # discontinue held products is caught. Scrapers with no detail fetch never report a ratio (no-op).
    delist_detail_failure_floor: float = 0.10


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
    # A specific category's pcat is category(<name>).pcat_id -- no per-product-type accessor.

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
    credentials_profile: str = env.getenv("BLOKPORT_AWS_PROFILE", "")
    # When true the image stage does not hit S3; it derives keys deterministically
    # and records them, but performs no network IO. Used when creds are absent.
    # Env-aware default: dev defaults dry (no accidental writes); PROD defaults to actually uploading, so a
    # prod deploy that forgets BLOKPORT_S3_DRY_RUN can't silently no-op every S3 write while emitting URLs
    # to objects that were never uploaded. Explicit BLOKPORT_S3_DRY_RUN still overrides in either env.
    dry_run: bool = _env_bool("BLOKPORT_S3_DRY_RUN", not IS_PRODUCTION)


# The active domain's generic material noun ('stone'/'wood'/'lime'), used to keep the de-watermark and
# image-classify prompts MATERIAL-neutral -- a wood pack must not instruct FAL to reconstruct a STONE
# pattern, nor score wood photos against a "natural stone slab" prompt. Resolved once at import from the
# deployment's DOMAIN_PACK.
_MATERIAL = active_pack().generic_material_word


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
    esrgan_model: str = env.getenv("BLOKPORT_ESRGAN_MODEL", "RealESRGAN_x4plus")
    esrgan_weights: str = env.getenv("BLOKPORT_ESRGAN_WEIGHTS", "")  # path override; else models/<model>.pth
    esrgan_tile: int = 512            # per-tile input for large images (0 = whole image)
    target_long_edge: int = 2048      # cap the 4x output at this long edge (INTER_AREA downscale)
    # The tone steps after the model (exposure lift + vibrance), on by default (the slab-photo recipe).
    # Off = detail only: Real-ESRGAN 4x + the long-edge cap, the photo's own tones kept (a bright studio
    # render on white would be darkened by the percentile stretch). SCRAPER_IMAGE_TONE=false turns it off.
    tone: bool = _env_bool("BLOKPORT_IMAGE_TONE", True)
    levels_lo_pct: float = 0.5        # exposure lift: black-point percentile
    levels_hi_pct: float = 99.6       # exposure lift: white-point percentile
    vibrance: float = 0.20            # restore colour muted by bad light (0 = off)
    # --- de-watermark (flagged sources only; needs the GPU imageproc extras) ---
    # De-watermark runs in the GPU reprocess (needs network + FAL_KEY, not a local model); FAL is billed
    # per megapixel ($0.05, 1 MP floor, rounds up) on the RAW image (before the ESRGAN upscale).
    dewatermark: bool = True
    # De-watermark is an INSTRUCTION edit (FAL FLUX Kontext), not a masked inpaint: Kontext edits the whole
    # image from the prompt and reconstructs the stone under the logo without the mask-fill's hallucinations.
    fal_model: str = env.getenv("BLOKPORT_FAL_MODEL", "fal-ai/flux-pro/kontext")
    # GENERIC fallback de-watermark instruction, used when a watermarked source has no per-source prompt
    # (SourceConfig.fal_prompt). It is source-AGNOSTIC on purpose -- it names no supplier -- so it is always
    # safe for any source (it removes whatever logo is present, never another supplier's specifically). Each
    # watermarked source SHOULD still set its own tuned prompt for best removal; this is the safe default.
    fal_prompt: str = (f"Remove any supplier watermark logo and any overlaid brand text from the surface of "
                       f"the {_MATERIAL} product, seamlessly reconstructing the natural {_MATERIAL} texture "
                       f"underneath. Keep the {_MATERIAL} colour, pattern, the size/label card and everything "
                       f"else in the photo identical to the original.")
    fal_seed: int = 0                 # fixed for best-effort reproducibility across re-runs
    fal_price_per_mp: float = 0.05    # USD per billed megapixel (rounds up, 1 MP floor)
    fal_max_usd: float = _env_float("BLOKPORT_FAL_MAX_USD", 150.0)  # per-run cost circuit breaker
    # Bounded attempts in the GPU reprocess before an image is TERMINALLY held. A completing image is
    # published (enhanced marker); one that still cannot complete after this many tries is written to the
    # discarded pool as a terminal HELD state, so every scraped image ends ready-or-held and the pipeline
    # always converges (ready + held == total) -- an unprocessable image never spins the indicator forever.
    enhance_max_attempts: int = 2
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
    classify_model: str = env.getenv("BLOKPORT_CLIP_MODEL", "openai/clip-vit-base-patch32")
    classify_margin: float = 0.85  # discard iff P(non-stone) >= this; conservative, keep when unsure
    classify_stone_prompts: tuple = (
        f"a photograph of natural {_MATERIAL}",
        f"a close-up photo of a {_MATERIAL} surface",
        f"a photo of {_MATERIAL} texture",
        f"a {_MATERIAL} product in a warehouse",
    )
    classify_nonstone_prompts: tuple = (
        "a document or technical spec sheet",
        "a price list or printed catalogue page",
        "a plain company logo on a blank background",
        "a screenshot of text or a table",
    )


IMAGE_MODES = ("passthrough", "local", "s3")


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

    mode: str = env.getenv("BLOKPORT_IMAGE_MODE", "passthrough")
    local_staging_dir: Path = REPO_ROOT / "state" / "image_staging"
    public_base: str = env.getenv(
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
    # A product is live only with a standardised photo (owner 2026-11-02: whatever has no good image is delisted until it
    # is fixed, one by one): without one it is served at stock 0 with reason 'no_photo' (Blokport hides it) and comes back
    # on the first produce after its photo is published. On in production; dev has standardised only a test set.
    require_standard_photo: bool = _env_bool("SCRAPER_REQUIRE_STANDARD_PHOTO", IS_PRODUCTION)
    # Number of numbered "Product Image N" columns a slab/tile product can fill. The pipeline
    # fills as many as the product actually has; the rest stay blank. Shared by the image-slotting
    # stage, the emit columns and the scrapers' per-product photo cap so they can never drift (the
    # template header must carry exactly this many Product Image columns -- asserted in tests).
    # A SAFETY CEILING, not a selection: every photo a vendor publishes is carried (Blokport's import,
    # pull sync and gallery are uncapped); 200 sits well above the largest set seen (114, a Sentas
    # scanner folder) and bounds what one runaway folder could cost in GPU time and hosting.
    product_image_slots: int = 200
    # One-time QUALITY upgrade of legacy variant textures, drip-fed by the produce. A product-backed
    # variant whose {Key}.png was made by an older model is re-made ONCE with the current best model and
    # recorded in the durable refreshed marker, so it never re-generates (or re-bills) twice. Capped per
    # run so a produce's FAL spend stays predictable while the backlog drains over successive runs.
    # Upgrade-then-list, never hold: a legacy texture still ships, it is just replaced within a few
    # produces. Ships OFF (0) -- enable deliberately with BLOKPORT_IMAGE_UPGRADE_BATCH once the marker
    # reflects everything already upgraded, else an enabled run re-does images that are already best.
    upgrade_batch: int = _env_int("BLOKPORT_IMAGE_UPGRADE_BATCH", 0)
    processing: ImageProcessingConfig = field(default_factory=ImageProcessingConfig)

    def __post_init__(self):
        # a typo'd mode used to fall through to passthrough silently (every product held no_image)
        if self.mode not in IMAGE_MODES:
            raise ValueError(f"image mode must be one of {IMAGE_MODES}, got {self.mode!r} (BLOKPORT_IMAGE_MODE)")


@dataclass(frozen=True)
class MatchingConfig:
    """Advanced variation tier (section 5A.2 tier 8, semantic). Off by default; a heavy optional
    dependency that feeds review only. (Tier 7 / Splink retired -- the alias_resolver logistic model
    fills the tier-7 role.)"""

    enable_semantic: bool = False  # tier 8, embedding nearest-neighbour suggestion
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
    variant_image_base: str = env.getenv(
        "BLOKPORT_VARIANT_IMAGE_BASE",
        f"https://{S3_BUCKET}.s3.{S3_REGION}.amazonaws.com/{ENV_SEGMENT}/variations/")
    # Base images per category derive from the CATEGORIES registry (image_prompts
    # selects by Key prefix; these accessors remain for any direct callers).
    # A specific category's base image is category(<name>).base_image -- no per-product-type accessor.

    # (Volume per kg (m³/kg) is per-category now; see CATEGORIES[*].volume_per_kg.)
    # a gap whose nearest existing variety scores at or above this is proposed as
    # an ALIAS of that variety rather than a new variant, to avoid creating
    # near-duplicate variants for what suppliers just renamed.
    alias_suggest_floor: float = 70.0
    # Tier-7 alias judge (matching.alias_resolver): character similarity of the cleaned names decides
    # alias-vs-new. At or above alias_char_floor (or a generic-only difference) it is a confirmed alias;
    # between the two floors it is a review; below, a new variety. A sibling ('Cristallo Divine' vs
    # 'Cristallo Bianco') shares a prefix but stays below the alias floor, so it is never auto-merged.
    enable_alias_model: bool = True
    alias_char_floor: float = 0.94
    alias_char_review_floor: float = 0.88


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
        return _catalog_source_file(self.backbone_filename)   # pack-namespaced (stone flat); never stone-fallback


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
    if env_var and env.getenv(env_var):
        return env.getenv(env_var)
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
_BY_LABEL_CF = {c.label.casefold(): c for c in CATEGORIES}  # plural label, casefolded


def category(name: str) -> "Category | None":
    # accept the singular name ('slab') OR the plural label ('Slabs') so a scraper that tags
    # its format in the plural ('Blocks'/'Tiles') still resolves the explicit tag instead of
    # silently falling through to the slab default.
    key = (name or "").strip().casefold()
    return _BY_NAME.get(key) or _BY_LABEL_CF.get(key)


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
    global _ENV_PCATS, _BY_NAME, _BY_LABEL_CF
    _ENV_PCATS = _env_category_pcats()
    rebuilt = tuple(replace(c, pcat_id=_pcat(c.label, c.pcat_env_var)) for c in _BY_NAME.values())
    _BY_NAME = {c.name: c for c in rebuilt}
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
    queue: str = env.getenv("BLOKPORT_GPU_QUEUE", "")
    job_definition: str = env.getenv("BLOKPORT_GPU_JOBDEF", "")
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
    queue: str = env.getenv("BLOKPORT_GPU_QUEUE", "")
    job_definition: str = env.getenv("BLOKPORT_GPU_JOBDEF", "")


@dataclass(frozen=True)
class LotConfig:
    """A lot is a set of ordinary products (pieces) sharing one lot_code (stages/lots.py). The code is
    `<label>-<uuid5(code_namespace, "<source_code>:<lot ref>")>`: unique, byte-identical on every piece,
    stable across runs. The namespace is fixed for good: changing it re-keys every lot on the next produce,
    so Blokport would see each as a new lot (guarded by a test)."""

    code_namespace: str = "be1ef5d0-ee2e-4157-b6ec-8870c49e7deb"
    max_code_length: int = 128   # Blokport's stated limit for lot_code (additional_data text field)


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
    lots: LotConfig = field(default_factory=LotConfig)

    # The source proven first (section 14). develi is absent from the supplied
    # data, so polonine is the clean named-variety proving source.
    spine_source: str = "polonine"


SETTINGS = Settings()


class STANDARDISE:
    """Every tunable of the photo standardiser (stone_pipeline/standardise). Flat on purpose: one namespace."""
    SAM_MODEL = "facebook/sam-vit-huge"   # Apache-2.0; the largest SAM: cleanest slab edges (T4: ~0.5 s encode, fp16)
    FOREIGN_MIN, FOREIGN_MAX = 0.0005, 0.03   # an excluded region this share of the slab = an object on it
    FOREIGN_THICK = 0.015          # ... and at least this thick (share of the slab's short side), not an edge sliver
    WHITE_PCT = 90                 # white balance + exposure read off the slab's brightest areas (this percentile)
    WHITE_TARGET_L = 92.0          # those areas are brought towards this L* (bright white, not clipped)
    DARK_STONE_L = 50.0            # brightest areas still darker than this L*: a dark stone (black, deep red), exposure kept
    EXPOSURE_MAX = 1.35            # exposure lift bound (about +0.43 stop); never lowered (highlights roll off instead)
    SHOULDER = 0.85                # linear values above this roll off softly towards white instead of clipping
    WB_STRENGTH = 0.8              # share of the measured cast removed
    WB_MAX = 0.08                  # never move a channel by more than this share (keeps a red stone red)
    WB_NEUTRAL_CHROMA = 12.0       # the cast is read only off pixels this close to grey (C*)
    WB_MIN_NEUTRAL = 0.05          # fewer neutral pixels than this share of the slab: a coloured stone, no cast fix
    CLARITY = 0.30                 # local-contrast boost of the veining (L* only), 0 = off
    CLARITY_SIGMA = 0.012          # its radius, share of the slab's long side
    VIBRANCE = 0.10                # chroma lift for dull colours, fading to none at VIBRANCE_FULL C*
    VIBRANCE_FULL = 40.0           # (a saturated red is not pushed further)
    WORK_LONG_EDGE = 2000          # process at this long edge
    MIN_SLAB_SHARE = 0.15          # the slab must cover at least this share of the photo, else not a full-slab shot
    TRIM_SHARE = 0.03              # attachments thinner than this share of the short side are not slab
    QUAD_EPS = (0.01, 0.02, 0.03)  # hull simplification ladder (share of the perimeter), finest first: a chipped
                                   # corner fits at the finest, a rough natural edge needs a coarser one
    EDGE_COVER_MIN = 0.85          # the four longest edges must be this share of the outline: a rectangle slab
    MAX_TILT_DEG = 35              # a corner fit implying more tilt than this is a misfit: clean-up only
    FLAT_SIGMA = 0.18              # illumination blur, share of the slab's long side
    FLAT_STRENGTH = 0.7            # 0 = no flat-field, 1 = full
    OCR_MIN_CONF = 30              # word confidence to join a mark (the card/logo box decides what is erased)
    OCR_ANCHOR_CONF = 80           # a mark is real text only with a word read this surely (labels/logos: 85-97;
    OCR_ANCHOR_LEN = 3             # ... of at least this many characters; stone texture reads as 2-letter noise <70)
    OCR_SHORT_CONF = 88            # a 2-character word anchors from this confidence (a "DC+" grade label reads at ~90)
    CARD_FILL = 0.7                # a bright component this rectangular around a word is a printed card
    CARD_MAX_SHARE = 0.08          # and no larger than this share of the photo
    CARD_SEARCH = 4.0              # look for a card outline within this many word-heights around a text cluster
    CARD_MAX_TO_TEXT = 4.0         # a card outline counts only up to this many times the area of the text it holds
    SIDE_RATIO_MAX = 2.5           # opposite sides may differ by this factor (a slab shot at an angle): steeper leaves the far
                                   # end too soft to show (19ab0177 at 3.1; 2.4 still sharp, adeebb0f)
    SIDE_RATIO_MEASURED = 1.35     # up to this the sides measure the slab's proportions; steeper, its real size does
    ASPECT_TOLERANCE = 0.10        # the real proportions may be this much narrower than a foreshortened photo shows
    OCR_CLUSTER = 1.5              # words within this many word-heights of each other form one mark
    OCR_LINE_GAP = 3.0             # a word on a surely-read word's text line, within this many word-heights, joins it
    OCR_LINE_HEIGHT = (0.5, 1.6)   # ... when its height is within this ratio of that word's
    MARK_PAD = 0.9                 # a mark's box is widened by this many word-heights on every side (card, logo)
    MARK_PAD_MAX = 0.012           # ... at most this share of the photo's long side (a code painted huge on the floor)
    FLARE_MAX_S = 40               # a lamp's reflection: colourless (HSV saturation, 0..255) ...
    FLARE_CONTRAST = 18            # ... this much brighter than the stone around it (pale stone leaves little room) ...
    FLARE_BASE_SIZE, FLARE_BASE_BLUR = 512, 41   # (that stone: a median over this filter size, on the photo at this long edge) ...
    FLARE_AREA = (0.00002, 0.012)  # ... this share of the slab's area ...
    FLARE_MIN_THICK = 5            # ... at least this thick (px) ...
    FLARE_SOLIDITY = 0.68          # ... compact: this share of its convex hull (a vein is not) ...
    FLARE_MAX_ELONGATION = 8       # ... at most this many times longer than wide (a strip light's tube; a vein is a line) ...
    FLARE_CORE_PCT = 90            # ... and its core (this percentile of its grey) near white ...
    FLARE_PEAK_MIN = 230           # ... at least this grey (pale flecks on a black stone reach 224) ...
    FLARE_STONE_PCT, FLARE_ABOVE_STONE = 99, 35   # ... and this far above the stone's own brightest (this percentile of it).
                                   # Measured 2026-11-01 on 19 photos: reflections core 232-254 and +37..+125 above it; white
                                   # veins/clasts on pale stone at most +27, pale flecks on black stone at most 224
    FLARE_GROW = 9                 # the fill also covers its glow, this many px wide
    MARK_ON_MIN = 0.5              # a word counts as on the slab when this share of it lies on the slab
    FILL_DETAIL_SIGMA = 0.003      # an erased area keeps LaMa's texture finer than this share of the long side; coarser tone is the stone's around it
    # --- marks ---
    # --- models and the join (blend), logos, views, what a photo shows (kinds) ---
    CARD_PROMPT = ("a stack of color sample cards. a sticker. a paper label. a printed sheet. a price tag. handwritten numbers. "
                   "a metal rack. a metal stand.")
    CARD_MIN_SCORE = 0.35              # a weaker detection fires on most photos ...
    LABEL_MIN_SCORE, LABEL_MAX_BOX = 0.25, 0.01   # ... a small one (this share of the photo) is tried from this score:
                                   # stickers on the stone scored 0.27-0.45, clean stone up to 0.33 (2026-11-01), so a refused
                                   # fill of one stays as shot, like a strap
    CARD_ON_SLAB = 0.6                 # a mark counts when this share of it lies on the slab
    STRAP_ON_SLAB = 0.6                # so does a strap (one sticking out above the slab still has most of it on the slab)
    INK_CHROMA = 15.0                  # writing found in part is completed by its ink: a stroke this vivid (Lab a*b*) ...
    INK_MIN_SHARE = 0.1                # ... when this share of the found mark is that vivid ...
    INK_APART = 20.0                   # ... and its ink this unlike the stone (so a blue stone adds nothing) ...
    INK_SAME = 12.0                    # ... the same ink within this ...
    INK_REACH = 0.15                   # ... this near the found mark (share of the long side)
    INK_MAX_YELLOW = 5.0               # ink no yellower than this (b*): marker blue -17..-31 and logo pink -1.6; an orange clamp +22..+36
    INK_MIN_PIECE = 15e-5              # a stroke at least this share of the photo: written characters 17-270e-5, crystals <= 14e-5
    CARD_MAX_BOX = 0.12                # a detected card or label is at most this share of the photo (else it is the slab)
    RETOUCH_PROMPT = ("Remove the straps, metal racks, sample cards, stickers, labels and handwritten numbers in front of or on the stone, showing the natural stone "
                      "beneath. Keep the stone, its colour, veining and edges and everything else exactly as they are. Do not add anything.")
    RETOUCH_GROW = 0.006               # a found card is widened by this share of the photo's long side
    RETOUCH_GUARD = 0.02               # the stone is compared this far away from what was found and from the slab's edge (share of the long side)
    RETOUCH_BROAD = 0.01               # at this scale, so fine redrawing does not count, only moved colour
    RETOUCH_MAX_DRIFT = 9.5            # the model may shift the stone's colour evenly this much (Lab): on 69 real crops good fills were <= 8.0, repaints >= 10.7
    RETOUCH_MAX_REACH = 0.08           # a fill may reach this far from real stone (share of the long side): straps and labels <= 0.063, card fans >= 0.105
    RETOUCH_MARGIN = 0.05              # the model sees this much around a mark (share of the photo's long side)
    RETOUCH_MIN_CROP = 384             # px: at least this much of the photo, so it sees the stone and not only the mark
    RETOUCH_MAX_ASPECT = 2.5           # and no narrower than this
    RETOUCH_MAX_SCALE = 2.0            # a small crop is enlarged at most this much for the model
    RETOUCH_TONE_SIGMA = 0.02          # a fill's tone coarser than this (share of the long side) is the real stone's around it; finer is the model's
    RETOUCH_RING = 0.02                # the fill is compared with the stone this far around it (share of the long side) ...
    RETOUCH_RING_INSET = 3             # ... both kept this many px inside the slab
    RETOUCH_MAX_COLOUR = 14.0          # its colour may differ this much (Lab) from the real stone around it ...
    RETOUCH_MAX_BUSIER = 2.4           # ... it may be this many times busier (median texture: 110 good real fills <= 1.84, invented 4.4+) ...
    RETOUCH_MAX_SMOOTHER = 2.2         # ... and this many times smoother (good fills down to 1/0.51): more is a smear
    RETOUCH_FEATHER = 2.0              # px: a lifted shadow's edge is softened this much
    RETOUCH_BLEND = 0.006              # beyond a mark, the edit fades into the real stone over this share of the long side
    CROP_ASPECT = (1.3, 2.0)           # a close-up cropped clear of its marks is landscape, this wide over high ...
    CROP_MARGIN = 0.06                 # ... this far from them (share of the long side): a card beside the found ones was missed ...
    CROP_MIN_SHARE = 0.5               # ... and at least this share of the photo, else it is held
    CROP_WORK = 256                    # px: the search runs on the mask at this long side
    RETOUCH_SEEDS = (20261030, 20261031, 20261101)   # a refused removal is tried again with the next draw of the model
    SHADOW_REACH = 0.03                # a mark's shadow is lifted this far from it (share of the long side): a stand's bar cast 0.023
    SHADOW_MIN_LIFT = 3.0              # where the photo is this much darker (L*) than the edit, beyond the edit's own shift ...
    SHADOW_MAX_HUE = 3.0               # ... and its colour (a*, b*) moved less than this: a missed black card moved more
    STUDIO_FRAME = (510.0, 110.0, 900.0)   # the studio camera (base image px): centre x, highest top edge, bottom edge ...
    STUDIO_HEADROOM = 70.0             # ... raised to keep this much white above a tall slab
    STUDIO_EDGE_LIGHT = 0.6            # the sawn edge facing away from the light, this bright relative to the face
    WHITE_MARGIN = 0.05                # on white, the slab's border (share of the canvas)
    WHITE_SEAM = 4                     # an open book's gap on white (px)
    WHITE_SHADOWS = ((26, 12, 0.30), (5, 3, 0.28))   # blur, drop (px), strength: a soft shadow and a tight contact one
    WHITE_TINT = (20, 41, 73)          # the shadow's colour, the site's navy
    EDIT_DIR = "/app/models/flux2-klein-4b"   # FLUX.2 Klein 4B (Apache-2.0), baked in the :standardise-edit image only
    EDIT_PIXELS = 1_000_000            # the model edits at about this many pixels
    EDIT_STEPS, EDIT_GUIDANCE = 4, 1.0
    EDIT_MAX_TOKENS = 128              # the instructions are short; the encoder's memory grows with this
    _KEEP = ("Keep the stone, its colour, veining, edges and everything else in the photo exactly as they are. "
             "Do not add anything.")
    EDIT_PROMPTS = {
        "strap": "Remove the two straps leaning across the stone slab, showing the slab's natural surface where they were. " + _KEEP,
        "logo": "Remove the company logo and its text from the surface of the stone, showing the natural stone beneath. " + _KEEP,
        "cards": "Remove the fan of sample cards lying on the stone, showing the natural stone beneath. " + _KEEP,
        # The showroom halls (assets/rooms) once: the user wants a matte floor, not a shiny or satin one (2026-11-01).
        "matte_floor": ("Make the shiny polished floor a matte grey concrete floor: no reflections of the lights, windows or "
                        "racks on it, no shine, an even matte surface with fine natural concrete texture. Keep the stone slabs "
                        "on their racks, the walls, windows, ceiling and lights exactly as they are. Do not add anything."),
    }
    TEST_PREFIX = "standardise-test"   # a test run writes under <env>/<this>/, never into improved/
    # The photo process (deploy.standardise_run, STANDARDISE_PROCESS.md) continues in the run tags the published results
    # came from, so no stored verdict, cleaned copy or choice record is orphaned: the standardiser's run over the scraped
    # photos, and the run over the hosted edit's cleaned copies, which also keeps the choice records and the review list.
    # Fixed here, not per deployment: a mistyped tag would re-pay every photo and unpublish the folder. Moving to new tags
    # is a migration (copy the choice records), never a setting. Dev's live choice came from v2-clean, prod's from v2-clean2.
    RUN_TAG = "v2"
    CLEAN_TAGS = {"dev": "v2-clean", "prod": "v2-clean2"}
    CLEAN_TAG = CLEAN_TAGS[ENV_SEGMENT]
    # On: the runner starts the photo process after each full produce (stage all, the owner's button), never after the
    # republish Blokport's Publish button runs (it must never find the run slot taken).
    AUTO = _env_bool("SCRAPER_STANDARDISE_AUTO", False)
    # The process works (GPU rounds, hosted calls) at most WORK_SECONDS, then reports, publishes and relinks in at most
    # FINISH_SECONDS; it refuses to start unless both fit under the runner's ceiling (RUN_TIMEOUT_SECONDS), which kills
    # a run without cleaning up.
    WORK_SECONDS = _env_int("SCRAPER_STANDARDISE_WORK_SECONDS", 7200)
    FINISH_SECONDS = 5400
    LOCK_HEARTBEAT_SECONDS = 300       # the run lock is rewritten this often while the process runs (its sign of life)
    SPEND_DIR = "spend"                # <env>/<TEST_PREFIX>/<this>/<YYYY-MM>/<run id>.json: each run's spend of the month
    JOB_PREFIX = "photos-"             # the process's Batch jobs: <this><run id>-..., so a later run finds what one left
    # The GPU's share of the month's budget: a job's running time at this price per vCPU-hour (g4dn.xlarge on demand in
    # eu-west-1, about 0.59 USD/h for its 4 vCPUs). An instance's boot before its first job is not counted.
    GPU_VCPU_HOUR_USD = _env_float("SCRAPER_STANDARDISE_GPU_VCPU_HOUR_USD", 0.147)
    # ... plus this much per job, for what its running time leaves out: the instance's boot, the image pull and the
    # pool's idle tail before it scales in (about 12 min on g4dn with the standardise image).
    GPU_JOB_ALLOWANCE_SECONDS = 720
    GPU_MIN_WINDOW_SECONDS = 1800      # no GPU round starts with less of the work window left (a boot alone takes minutes)
    # A stopped round's jobs are asked every STOP_POLL_SECONDS, at most STOP_WAIT_SECONDS, until they have stopped, so
    # their real stop times are counted; inside the 100 s stop grace on a SIGTERM.
    STOP_WAIT_SECONDS, STOP_POLL_SECONDS = 60, 5
    CUTOUT_WORKERS = 4                 # 3D cut-outs made at once by publish (full-size slabs in memory, on the 6 GB service)
    SHEET_KEY = "scraper/to_upload/3_products_all.csv"   # the product sheet, under <env>/
    # Each listing's own photos in its order ({Product Handle: [[source, sha], ...]}), written by the produce beside the
    # sheet: the sheet's image columns carry only the chosen photo's views once it is published, so the photo process
    # and its GPU job read the listing's photos from here.
    PHOTOS_FILE = "3_products_photos.json"
    PHOTOS_KEY = "scraper/to_upload/" + PHOTOS_FILE
    # The 3D slab (the "Slab 3D on Blokport" contract, version 1): the clean slab a run saved for a whole slab (<sha>.slab.png:
    # a single slab, or one half of an open book, each half a whole slab) is published as a WebP cut-out and a JSON outline;
    # Blokport's viewer extrudes the outline to the product's thickness with the photo on the face. Only a slab product
    # gets one (not a block, tile or close-up).
    CUTOUT_VERSION = 1
    CUTOUT_FORMAT = "slab"             # the product format (format_value, any case) that gets a 3D slab
    CUTOUT_LONG_SIDE = 2048            # px, the contract's maximum
    CUTOUT_WEBP_QUALITY = 86           # the quality the approved review viewer used
    CUTOUT_ALPHA_CUT = 127             # alpha above this is stone
    CUTOUT_MAX_POINTS = 64             # the contract's outline limit
    # The outline is simplified to this share of its length (the approved viewer), coarser by CUTOUT_EPSILON_STEP until
    # it has at most CUTOUT_MAX_POINTS points: a ragged natural edge keeps its shape, only finer.
    CUTOUT_EPSILON, CUTOUT_EPSILON_STEP = 0.0015, 1.5
    CUTOUT_EDGE_WHITE = 0.25           # the sawn edge: the stone's mean colour mixed with this much white (paler, duller)
    # The last check before a finished photo is published (user 2026-11-01: GPU first, a strict check, the hosted model for
    # the rest): a vision model sees the original and the catalog image and passes or fails it with reasons. Opus matched the
    # user's own labels best (23 of 25 defects, 6 of 6 flagged; Haiku 16, Sonnet 19). One good photo per listing is enough:
    # its photos are judged in order and judging stops at the first pass, at most JUDGE_MAX_PER_LISTING of them.
    JUDGE_URL = "https://fal.run/openrouter/router/vision"
    JUDGE_MODEL = "anthropic/claude-opus-5.5"
    # The bar the prompt sets. 2 (user 2026-11-02, Namib Bianco: "if the final result looks like the FAL image I would
    # accept that, I rather keep the product"): a tiny remnant at the very edge no longer fails. A stored verdict carries
    # the version it was made under; a FAIL made under a lower one is judged again (a pass stays a pass: the bar only
    # moved towards passing, so nothing that passed before can fail now). 3 (owner 2026-11-02, hidden listings whose
    # results looked fine: black-queen 7029-27, bianco-river 7099, amasya 980): the slab's proportions are corrected to its
    # real dimensions on purpose (never wrong_shape), and only what a buyer would see at catalog size fails.
    JUDGE_VERSION = 3
    JUDGE_REASONING = True             # the endpoint refuses this model without it
    JUDGE_MAX_TOKENS = 6000            # reasoning included
    JUDGE_LONG_SIDE, JUDGE_JPEG_QUALITY = 1024, 88   # each image is sent at this long side
    JUDGE_TIMEOUT = 300                # s per call
    # A run over the listings still without a good photo goes further down each listing (prod 2026-11-02: 365 such listings
    # had 2,025 photos past the third, never tried): a photo costs a GPU pass and one judge call, far less than an edit.
    JUDGE_MAX_PER_LISTING = _env_int("SCRAPER_STANDARDISE_JUDGE_MAX", 3)
    JUDGE_KINDS = ("light_reflection", "scene_reflection", "bad_fill", "leftover_mark", "not_one_slab", "wrong_shape", "other")
    JUDGE_PROMPT = """You are the last quality check before a natural-stone product photo is published in a marketplace catalog.
Image 1 is the supplier's original photo. Image 2 is our processed catalog image of it.
- For a SLAB (or two bookmatched slabs side by side): the slab is cut out of image 1, squared, cleaned and placed
  upright in a showroom. Its chrome feet / supports at the bottom belong to the showroom stand. The whole slab is also
  stretched or squeezed to its real dimensions, so its width-to-height ratio may differ from image 1: that alone is
  never wrong_shape. But every edge and corner the slab has in image 1 must still be in image 2.
- For a BLOCK or TILE: image 2 is the product cleaned in its own surroundings; there is no showroom. A quarry
  block's painted numbers and markings, its lifting cables and chains, and the quarry or yard around it are normal
  and never a problem.
Judge ONLY the stone product in image 2 (its surface, its outline and what is on it), using image 1 to tell what is
real stone and what is not. The showroom room, floor, stand and feet are fixed and never a problem.

Look at image 2 as a buyer sees it at catalog size (a card about 400 pixels wide). FAIL it only if that buyer would
clearly notice any of these on the stone:
- light_reflection: lamp spots, rows of ceiling lights, light tubes or bright glare mirrored on the polished surface
- scene_reflection: windows, the hall, people or objects clearly mirrored in the stone
- bad_fill: a patch, rectangle or smear whose tone, brightness or texture does not match the stone around it
  (where a label, logo, card or strap was removed)
- leftover_mark: a label, sticker, card, logo, writing, tape, protective film or strap still on the stone (a tiny
  remnant at the very edge of the slab, hardly visible at catalog size, is not a problem)
- not_one_slab: two or more slabs shown as one (stacked with an offset, a second slab's edge visible), or another
  stone taken in
- wrong_shape: the product cut off, part of it missing, warped, or something in front of it cut into its outline
  (for a SLAB, the whole slab at a different width-to-height ratio is the scaling and never wrong_shape; an edge,
  corner or strip of the slab in image 1 that is missing from image 2 is wrong_shape)
- other: any other clear defect of the image (not of the stone itself)

PASS when the stone looks clean and natural at catalog size. Natural features of the stone (veins, cracks, holes,
colour changes, breccia, resin, sheen, rough or chipped edges) are not problems, nor is a faint reflection, soft sheen
or small faded spot a buyer would not notice at catalog size.
Your whole answer is one JSON object and nothing else:
{"verdict": "pass" | "fail", "problems": [{"kind": "<one of the kinds>", "where": "<short>"}]}"""
    # A listing with no photo that passes: the hosted edit models clean ONE of its photos (the first that can be fixed by
    # cleaning) in turn until a copy passes the clean check; that copy then goes through the standardiser like any photo and
    # the judge again. Order by cost per passed copy (prod run 2026-11-02, 1,013 photos): Nano Banana 2 ($0.12) passed 55%
    # even on the photos Kontext had failed, Kontext ($0.15) 31% first, FLUX 3 ($0.15) 37% last; the cheapest and most
    # successful goes first (the three in turn reach the same photos either way). The queue, not the sync gateway: a slow
    # model outlasts it (504) and the paid call is lost.
    CLEAN_QUEUE_URL = "https://queue.fal.run"
    # Straps too (sample 2026-11-01: 27 of 42 failures were the GPU's own strap fills, light bands and clamp remnants, on
    # copies the hosted edit had otherwise cleaned): the hosted edit takes off everything foreign, so the retouch finds
    # nothing left to fill.
    CLEAN_INSTRUCTION = ("Remove only these from the polished stone slab: lifting straps, belts and their corner clamps or "
                         "protectors; reflections of lamps, light tubes, windows and the room mirrored on its surface; and any "
                         "label, sticker, sample card, tape, protective plastic film, writing or logo on it. Where they were, "
                         "show the slab's own stone, "
                         "continuing its pattern. Keep the stone's colour, veins, pattern and texture exactly as they are, and keep "
                         "the slab's outline and the rest of the photo unchanged.")
    # The instruction's version (2: protective film too). A stored attempt that failed under a lower one is made again with
    # the current instruction; a passed one stands.
    CLEAN_VERSION = 2
    CLEAN_MODELS = (                   # (name, endpoint, extra input); the photo goes in as image_url or image_urls
        ("nano2", "fal-ai/nano-banana-2/edit", {"resolution": "2K", "output_format": "jpeg", "seed": 1}),
        ("kontext", "fal-ai/flux-pro/kontext", {"output_format": "jpeg", "seed": 1}),
        ("flux3", "blackforestlabs/flux-3/edit-image", {"resolution": "1.5k", "output_format": "jpeg"}),
    )
    CLEAN_SINGLE_IMAGE = ("kontext",)  # these take one image_url; the others a list, image_urls
    CLEAN_SEND_LONG = 2048             # the photo is sent at this long side
    CLEAN_TIMEOUT, CLEAN_POLL = 600, 3 # s: give up on a queued edit after this long, asking every this many s
    # Problems a cleaned copy can fix; a photo held or failed for anything else (an outline the standardiser could not
    # find, stacked slabs) is not worth an edit.
    CLEAN_FIXES_KINDS = ("light_reflection", "scene_reflection", "bad_fill", "leftover_mark")
    CLEAN_FIXES_HELD = ("retouch:", "logo:")   # held reasons (their start) a cleaned copy removes the cause of
    CLEAN_FAILED_CODES = (400, 422)    # a queued edit the model refused for good; any other error is loud and retried
    CLEAN_CHECK_PROMPT = """Image 1 is a supplier's photo of a natural-stone slab. Image 2 is an edited copy meant to remove lifting
straps with their clamps, reflections (lamps, light tubes, windows, the room mirrored on the polished stone) and
foreign marks (labels, stickers, sample cards, tape, protective film, writing, logos) from the stone, and nothing else. Judge image 2
on two questions:
1. clean: are all such straps, clamps, reflections and marks gone from the stone in image 2? (true/false)
2. faithful: is the stone itself the same stone as in image 1 (same colour, veins, pattern and texture, nothing
   invented, nothing smeared or blurred, slab outline unchanged)? (true/false)
Your whole answer is one JSON object and nothing else:
{"clean": true|false, "faithful": true|false, "problems": "<short, empty if none>"}"""
    # The month's (UTC) budget of an environment's photo process, all runs together: hosted calls (judge as billed, edits
    # at list price) and GPU time (GPU_VCPU_HOUR_USD). A call or a GPU round that would take the month's total past it is
    # not made. No default: unset, the process does not start.
    BUDGET_USD = _env_float("SCRAPER_STANDARDISE_BUDGET_USD", None)
    # A judge call's cost reserved before it is made: its worst case (JUDGE_MAX_TOKENS out at $20/M plus two images in at
    # $4/M, Opus 5.5), so parallel calls can never together pass the budget; Opus bills ~0.016 on average.
    JUDGE_PRICE_USD = 0.14
    CLEAN_PHOTOS_PER_LISTING = 1       # one good photo is enough: only the first fixable photo of a listing is cleaned
    # Off: a run sends no photo to a hosted edit (a judge-only round: GPU, judge, cleaned copies already made).
    CLEAN_ENABLED = _env_bool("SCRAPER_STANDARDISE_CLEAN", True)
    # The process (deploy.standardise_run) sends the GPU work to AWS Batch in slices of RUN_SLICE photos, each a retouch job
    # then a standardise job. The GPU pool's ceiling is raised to RUN_GPU_VCPUS while it works and always set back to
    # IDLE_GPU_VCPUS after (the pool scales to zero when idle; the ceiling caps what a burst can cost).
    # The size of a run's GPU burst is set per run (a full environment overnight wants more GPUs and smaller slices than
    # a test): the ceiling is the account's G-instance quota at most, and is always set back to IDLE_GPU_VCPUS.
    RUN_SLICE = _env_int("SCRAPER_STANDARDISE_RUN_SLICE", 150)
    RUN_GPU_VCPUS = _env_int("SCRAPER_STANDARDISE_GPU_VCPUS", 128)
    IDLE_GPU_VCPUS = 8
    RUN_POLL_SECONDS = 60              # how often a run asks Batch whether its jobs are done ...
    RUN_MAX_WAIT_SECONDS = 12 * 3600   # ... and how long at most per wave of jobs (the slices that run at once) before it
    #                                    stops them and fails (jobs stuck, no GPU quota); above a healthy slice's longest
    RETOUCH_TIMEOUT, STANDARDISE_TIMEOUT = 14400, 21600   # s a Batch job may take before Batch stops it
    GPU_JOB_VCPUS = 4                  # each GPU job's vCPUs (the standardise job definition: 4 vCPU, 1 GPU, no retries):
    #                                    RUN_GPU_VCPUS // this jobs run at once
    SLICE_MAX_STORAGE_FAILURES = 3     # storage errors in a row before a GPU slice stops (the next run retries)
    SELECT_WORKERS = _env_int("SCRAPER_STANDARDISE_WORKERS", 12)   # listings worked on at once (hosted calls only)
    # Per edit at the sizes above, an upper bound (fal.ai/pricing: Kontext per megapixel rounded up, Nano Banana 2 per image
    # with 2K above base, FLUX 3 per megapixel), so the cap stops early rather than late.
    CLEAN_PRICE_USD = {"kontext": 0.15, "nano2": 0.12, "flux3": 0.15}
    # The revision each model is baked at in the image (Dockerfile): the jobs run offline, so "latest" cannot resolve.
    SAM_REVISION = "87aecf0df4ce6b30cd7de76e87673c49644bdf67"
    DETECTOR_REVISION = "12bdfa3120f3e7ec7b434d90674b3396eccf88eb"
    CLIP_REVISION = "3d74acf9a28c67741b2f4f2ea7635f0aaf6f0268"
    BLEND_REVISION = "115134f363124c53c7d878647567d04daf26e41e"
    BLEND_VAE_REVISION = "207b116dae70ace3637169f1ddd2434b91b3a8cd"
    CLIP_MODEL = "openai/clip-vit-base-patch32"
    BLEND_MODEL = "diffusers/stable-diffusion-xl-1.0-inpainting-0.1"   # Open RAIL++-M
    BLEND_VAE = "madebyollin/sdxl-vae-fp16-fix"   # the stock SDXL VAE overflows in fp16 (black images)
    BLEND_PROMPT = ("photograph of a large polished natural stone slab standing on a black steel stand in a stone showroom, sharp "
                    "natural slab edge, realistic contact shadow on the polished concrete floor")
    BLEND_NEGATIVE = "halo, glow, blur on the slab, text, logo, people, cgi, 3d render, illustration"
    BLEND_SIZE = (1216, 832)           # an SDXL training bucket, the canvas's shape
    BLEND_STEPS, BLEND_GUIDANCE, BLEND_STRENGTH = 30, 5.0, 0.6
    BLEND_SEED = 20261030              # fixed: same input, same output
    BLEND_FEATHER = 2.0
    BAND_OUT, BAND_IN = 44, 4          # the repainted band: this far outside the slab and this far inside it (px)
    BAND_FADE = 0.7                    # it fades out over this share of its width, so no rectangle shows on the floor
    BAND_CHIP_SOFT = 2.0               # a real chip is never repainted (edge softness, px)
    OPAQUE_MAX_CHANGE = 12             # a pixel changing less than this with the room behind it is slab or stand
    STAND_MIN_CHANGE = 6               # a pixel changing more than this without the stand is the stand or its shadow
    PHOTO_KINDS = {
        "slab": "a photo of one large polished stone slab standing upright, the whole slab in view",
        "close-up": "a close-up photo of the surface texture of polished stone",
        "open book": "a photo of two bookmatched stone slabs standing side by side",
        "pile": "a photo of many stone slabs stacked in piles or racks in a warehouse",
        "hangar": "a wide photo of a warehouse or factory hall",
        "interior": "a photo of a finished kitchen, bathroom or room with stone surfaces",
        "other": "a document, logo, price list or chart",
    }
    SKIP_KINDS = ("pile", "hangar", "interior", "other")
    SKIP_MIN_CONFIDENCE = 0.6          # only a sure one is skipped here; the outline's checks catch what is left
    LOGO_BOX = (0.6055, 0.2051)        # the logo is searched in this box round the photo's centre, shares of its width
    LOGO_EDGE = 30                     # the box's outer px count as stone around the logo
    LOGO_ECC_STEPS, LOGO_ECC_EPS = 150, 1e-5   # alignment
    LOGO_ALIGN_MIN = 0.80              # below this correlation the copies do not line up: held
    LOGO_DIFF_MIN = 16.0               # the logo differs from the cleaned copy by this much (Lab) once tone is matched
    LOGO_TONE_SIGMA = 75.0             # broad tone of the cleaned copy matched to the original at this scale (px)
    LOGO_JOIN = 41                     # its parts join across gaps this wide (px)
    LOGO_PAD = 8                       # the rectangle is widened by this on every side (px)
    LOGO_FULL, LOGO_RIGHT = 3.2, 1.2   # narrower than this many times its height: the grey word was missed, reach this share further right
    LOGO_MAX_SHARE = 0.85              # a rectangle covering more of the box than this: the copies differ everywhere, held
    LOGO_FEATHER = 2.0
    LOGO_HUE, LOGO_MIN_SAT, LOGO_MIN_VAL = (160, 3), 110, 110   # the logo's pink (HSV, 0..180 / 0..255)
    LOGO_MAX_LEFT = 40                 # more pink pixels than this left after removal: held
    CLOSEUP_SHARE = 0.45               # a close-up shows this share of the slab's width
    SEAM_BAND = (0.46, 0.54)       # an open book is two equal slabs from one block: its seam is central
    SEAM_MIN = 6.0                 # robust z-score of a joint column (edge change + dark gap)
    JOINT_EDGE = 0.01              # joints are looked for inside this margin of the outline's ends
    JOINT_NMS = 0.03               # two joints closer than this share of the width are one
    SLIVER_MAX = 0.12              # at a frame-touching end, a joint within this share cuts off a neighbour's edge
    SEAM_MIN_END = 4.0             # ... at this lower score (a slab meeting a pale post is a soft joint)
    MIRROR_MIN = 0.08              # vein mirror correlation across the seam (single slabs: ~0.01)
    JOINT_BAND = 0.25              # mirror test on this share of each slab's width at the joint
    JOINT_HEIGHT = 384             # both slabs resampled to this height for the joint test
    JOINT_SIGMA = (0.007, 0.04)    # band-pass of the veining compared at the joint, share of the height
    JOINT_MIRROR_MIN = 0.15        # mirrored joint correlation of a bookmatch (Sentas pair: 0.27)
    JOINT_MARGIN = 0.10            # and at least this much above the unmirrored control
    PAIR_ASPECT = 2.4              # an outline this wide is two slabs: a strong seam alone splits it
    SAME_IOU = 0.5                 # a probed mask overlapping ours this much is our own slab, not a partner
    NEIGHBOUR_GAP = 0.04           # the partner's near edge within this share of the slab width of ours
    BESIDE_GAP = 0.15              # a separate slab beside ours (a rack's next slab, a pole between: 39212b82 ~0.10) within this:
                                   # it explains a detector box reaching past our slab
    NEIGHBOUR_SIZE = (0.88, 1.12)  # and its width and height within this ratio of ours (equal slabs)
    DETECTOR = "IDEA-Research/grounding-dino-base"   # Apache-2.0 open-vocabulary detector, our GPU
    DETECT_PROMPT = "a stone slab."
    DETECT_MIN = 0.3               # detector box confidence
    EXTEND_MAX = 0.10              # the detector's box may reach past our outline by at most this share of its size,
                                   # else the outline is partial (straps in front, a rough natural edge)
    STRAP_PROMPT = "a wooden plank. a strap."
    STRAP_MIN_SCORE = 0.20         # detector confidence for a strap (lower than for slabs: the shape tests below guard)
    STRAP_PAD = 0.012              # strap mask widened by this share of the photo's long side (edges, shadow)
    STRAP_MIN_HEIGHT = 0.6         # a strap runs along at least this share of the slab's height ...
    STRAP_MAX_WIDTH = 0.25         # ... is at most this share of its width (else it is the slab itself) ...
    STRAP_ELONGATION = 6.0         # ... and its mask is at least this many times longer than wide
    STRAP_PIECE_MIN = 0.02         # slab pieces left beside the straps count when this share of the slab
    FRAME_EDGE = 0.01              # a slab corner this close (share of the photo) to a frame edge its stone runs into: cut off
    FRAME_TOUCH = 0.02             # the stone runs into a frame edge when it covers this share of that edge's outermost line
                                   # (measured 2026-11-01: cut off 3.8-74%, framed tightly 0%)
    # --- outline ---
    ROOM_COUNT = 3                     # rooms/<1..n>.jpg, and far_<1..n>.jpg (the same hall from twice as far, for an open book)
    FAR_ROOM_BLUR = 3.0                # px: the far hall softened like a background behind slabs in focus
    OUT_W, OUT_H = 2048, 1365           # every image: the storefront's 3:2 frame (card, product page, viewer) at the 2048 px it stores
    MARGIN = 0.035                     # showroom border left, right and above the slab(s), share of OUT_W
    FLOOR_SHARE = 0.13                 # the one floor line: floor below every slab, share of the canvas height
    SHADOW = 0.35                      # contact shadow strength under each slab
    TWO_STONES_GAP = 150.0             # a cut-out whose light and dark halves differ by more than this (8-bit Lab) ...
    TWO_STONES_PIECE = 0.05            # ... with the odd half's largest piece this share of it ...
    TWO_STONES_BORDER = 0.3            # ... running along this share of its border took in another slab (178/0.72 vs <=119/0.35 on 72)
    TWO_STONES_WORK = 400              # px: judged at this long side ...
    TWO_STONES_BLUR = 3.0              # ... on the broad colour, not the grain
    CONTACT = 0.6                      # under the slab the floor is this much darker (no light gets there) ...
    CONTACT_DEPTH = 0.04               # ... over this share of the canvas height ...
    CAST = 0.55                        # ... and a soft shadow spills past the slab's ends: this strong ...
    CAST_REACH = 0.07                  # ... reaching this share of the canvas width
    TRIM_REACH = 0.03                  # an edge is tightened by at most this share of the slab
    TRIM_BAND = 0.03                   # the stone it is compared with: this wide, just inside that reach ...
    TRIM_SMOOTH = 0.05                 # ... averaged along the edge over this share of it
    TRIM_UNLIKE = 11.0                 # unlike the stone from this colour difference (Lab) ...
    TRIM_L_WEIGHT = 0.35               # ... brightness counting this much (an edge's light and shade are still the stone)
    TRIM_OWN, TRIM_OWN_PCT = 1.2, 98   # ... and from this many times what the stone itself varies by (this percentile), so a busy stone is left alone
    TRIM_SOFT = 0.006                  # the cut is smoothed along the edge over this share of it
    TRIM_LINE, TRIM_LINE_PCT = 1.6, 99.5   # a physical edge is this many times this percentile of the stone's own change further in
    TRIM_MIN_LENGTH = 0.2              # a cut must run along at least this share of the edge, else it would leave a nick
    TRIM_RUN = 0.03                    # a cut must run along this share of the edge (a vein crossing the edge does not)
    NOTCH_FILL = 0.08                  # bites in an edge narrower than this share of the slab's height are filled
    OUTLINE_CLOSE = 0.012              # notches thinner than this share of the photo's long side are filled, wider ones are real
    OUTLINE_HOLLOW_MAX = 0.02          # a cut-out missing more than this share of its convex hull has a stand in front of it
                                       # (measured 2026-11-01 on 320 slabs: normal <= 0.0064, a stand cut out 0.108-0.153)
    EDGE_INSET = 2                     # the outline is taken this many px inside the photo's mixed edge pixels
    EDGE_SOFT = 0.6                    # the slab's edge softness (px): crisp
    FACE_WIDTH = 11                    # the stone's thickness on the canvas: about 2 cm of a 3 m slab (px)
    FACE_SHADE = (0.62, 0.34)          # its edge face in the side light: this bright beside the slab, this at its far edge
    EDGE = 0.45                        # the slab's bottom edge face (its thickness): this bright, relative to the stone ...
    EDGE_HEIGHT = 0.004                # ... this high, share of the canvas height
    FOOT_FADE = (0.10, 0.03)           # the stone's lowest part, near the floor, gets less light: by this much, over this share of the height
    AMBIENT_FALL = 0.035               # the slab's soft shade on the floor in front of it, share of the canvas height
    JOINT = 3                          # an open book's seam: this many dark pixels between the two slabs
    JOINT_COLOUR = (28, 28, 30)
    JOINT_TRIM = 0.006                 # each slab's edge face at the seam (catches the light) trimmed, share of its width
    DETAIL_SCALE = 0.8                 # ESRGAN contributes detail finer than this many source pixels
    ESRGAN_TILE = 512                  # ESRGAN input tile (bounds GPU memory on large slabs)
    CACHE_TAG = "v13-reflections-by-model"      # squared-slab cache, per pipeline version
    # --- layout ---
    DEFAULT_SLAB = (3.04, 1.87)        # median prod slab L x W (m): proportions for test photos with no product
    CLOSEUP_BORDER = 0.70              # stone along at least this share of EACH photo border: a close-up ...
    CLOSEUP_INSET = 0.02               # ... read this share in from the edge (SAM's masks stop a few px short of it)
    CLOSEUP_FLOOR_PROMPT = "a floor."  # a close-up shows no floor: the detector boxes its stone as "floor" from the top ...
    CLOSEUP_FLOOR_MIN = 0.35           # ... (real close-ups 0.41-0.91, box from the top), but under a slab the real floor
    CLOSEUP_FLOOR_TOP = 0.30           # ... starts lower (best box from 0.46-0.76 H on 56 Sentas warehouse photos)
    SHARP_MIN = 15.0                   # variance of the Laplacian at 1024 px below this: blurred, skipped (calm sharp marble ~32, blurred ~4)
    # --- closeup ---
    BLOCK_PROMPT = "a stone block."
    CABLE_PROMPT = "a rope. a cable."
    BLOCK_MIN_SHARE = 0.10             # a detected block must cover this share of the photo to measure light on it
    CABLE_ELONGATION = 6.0             # a cable mask is at least this many times longer than it is wide
    CABLE_MAX_SHARE = 0.04             # ... and no larger than this share of the photo (not the block itself)
    CROP_NOTE_MIN = 0.01               # the log says so when the canvas crop cuts more than this share of a block
    # --- scene ---
