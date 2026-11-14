"""Global configuration block for the stone import pipeline.

Operating principle 1: no argparse. All paths, ids, thresholds, and tunables
live here (global defaults) or in config/sources.yaml (per source). Nothing is
inlined in stage or resolver code.

Operating principle 8: the template is the schema authority. Column names and
order are read from the live template at emit time, never hardcoded here. This
file only points at where that template lives.

All backend ids below belong to one backend environment (section 3.2). The
defaults are the dev-staging values observed in the real upload example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
# The variety base's file name: the same name in from_medusa/<env>/ (the base a produce reads and settles) and in
# to_upload/<env>/ (the copy of it published as a manifest-listed deliverable).
VARIANTS_BASE_FILE = "variants_export_base.csv"

# Blokport change 1 (VE2) adds status/internal columns to attributes.csv. While False an absent column reads as
# active and not internal; step C flips this once Blokport change 1 is live on prod, so a missing column fails loud.
ATTRIBUTES_STATUS_REQUIRED = False
# The same change adds a status column (active|archived) to Medusa's variants_export.csv, flipped with it in
# step C. Only that export is held to it: the scraper's own base files never carry the column.
VARIANTS_EXPORT_STATUS_REQUIRED = False


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean env var (1/true/yes/on); fall back to default when unset. Delegates to the one
    boolean-env parser in core.env so the truthy vocabulary lives in a single place."""
    return env.env_bool(name, default)


def _env_int(name: str, default: int | None) -> int | None:
    """Read an integer env var: the default ONLY when unset. A set-but-malformed override fails loud at
    config import, naming the variable: an operator who typed SCRAPER_STANDARDISE_RUN_SLICE=abc meant a
    value, and silently running on the default is the no_image incident class (a run proceeding on a value
    nobody set)."""
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
# ... and a PROD run never on the dev bucket, even set explicitly: that passes the guard above (it is set) and the
# brand-prefix guard below (it is blokport's), and would write prod data into dev. Checked here, at import, so no entry
# point (produce, image stage, image wipe, sync or config server) can reach S3 with it.
if IS_PRODUCTION and S3_BUCKET == _DEV_S3_BUCKET:
    raise RuntimeError(
        f"production run targeting the DEV S3 bucket {_DEV_S3_BUCKET} -- refusing to run. Set SCRAPER_S3_BUCKET to "
        "the production bucket.")
S3_REGION = env.getenv("BLOKPORT_S3_REGION", "eu-west-1")
# The AWS clients' configurations (io/storage.aws_client presets). BOUNDED: the image and catalog tools, timed and
# retried so a network stall cannot hang a build. FAIL_FAST: the ledger snapshot, whose stop-path save must not burn
# the ECS stop timeout on a hung call. The photo process's retries are STANDARDISE.AWS_MAX_ATTEMPTS.
AWS_BOUNDED_CONNECT_SECONDS = 5
AWS_BOUNDED_READ_SECONDS = 30
AWS_BOUNDED_MAX_ATTEMPTS = 3
AWS_FAIL_FAST_CONNECT_SECONDS = 3
AWS_FAIL_FAST_READ_SECONDS = 10
AWS_FAIL_FAST_MAX_ATTEMPTS = 2

# The config runner's ceiling on one run: it kills a run past it as wedged (Terraform sets 4 h on the service). The
# photo process fits its work window inside it and treats a run lock not refreshed for this long as a killed run's.
RUN_TIMEOUT_SECONDS = _env_int("SCRAPER_RUN_TIMEOUT_SECONDS", 7200)
# On SIGTERM (ECS stops the task) the config server ends the running run within this one deadline: the child's grace to
# clean up after its SIGTERM, a kill and the clearing of what a killed photo run left, and its watcher finishing the run
# record. It leaves 40 s of the config container's stopTimeout (120 s, the Fargate maximum) for the last config save.
# The same deadline bounds a photos run giving way to another stage.
RUN_STOP_GRACE_SECONDS = _env_int("SCRAPER_RUN_STOP_GRACE_SECONDS", 80)
# The config runner keeps this many run records in memory for GET /run/<id> (the durable history is the run_log), so a
# long-lived server never grows without bound; and this many last log lines of a run on its record (the failure cause).
RUN_HISTORY_MAX = 200
RUN_LOG_TAIL_LINES = 40
# A thin copy of a protected base file (Medusa mid-reset, or a produce on a thin input) replaces the fuller one only
# when it keeps at least this fraction of its rows: the S3 input fetch (stone_pipeline.io.medusa_inputs) and the base write-back
# (reference.sync_variants_base) both refuse a collapse below it.
THIN_COPY_MIN_FRACTION = 0.5

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


# Per-material data (the catalog_source/ backbones, origin map, supplier overrides and new-variety deltas; the
# reference/ synonyms, type densities and standard slab areas) is MATERIAL-specific. The stone pack (historical) keeps
# it flat at <base>; any OTHER pack MUST ship its own <base>/<pack>/ (catalog_source/<pack>/, reference/<pack>/) and
# NEVER falls back to stone's -- so a wood/lime run reads its own data or gaps/fails loudly, never silently loads stone
# varieties/origins/synonyms/densities.
_STONE_PACK = "stone"


def _pack_namespaced(base: Path) -> Path:
    """The active pack's folder of per-material data under `base`: `base` itself for stone, `base/<pack>` otherwise."""
    pack = active_pack().name
    return base if pack == _STONE_PACK else base / pack


def _catalog_source_file(filename: str) -> Path:
    return _pack_namespaced(WORKSPACE_ROOT / "catalog_source") / filename


def _reference_file(filename: str) -> Path:
    return _pack_namespaced(REPO_ROOT / "reference") / filename

# Last-resort attribute defaults are DOMAIN VALUES, so they live in the active pack, not here:
# config/domains/<pack>.yaml -> last_resort_finishes / last_resort_quality / block_finish (read via
# domain.active_pack() in stages/normalize.py). Field NAMES stay the Medusa contract; the VALUES are pack.
# Colour is NOT last-resorted here (it inherits the variety's texture-classified colour, 'Natural' floor).


# --- Product descriptions -------------------------------------------------------
# A supplier description passes only as plain English prose: its share of these English function words must reach
# DESCRIPTION_MIN_ENGLISH_SHARE (prod: English 0.20 to 0.45; the Spanish zucchi texts 0.014; internal notes 0.0).
# 'no' is deliberately absent: it is also Spanish and Italian.
DESCRIPTION_FUNCTION_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in", "is", "it", "its", "of",
    "on", "or", "that", "the", "this", "to", "which", "with",
})
DESCRIPTION_MIN_ENGLISH_SHARE = 0.10


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
    # curate's raw new + alias-update delta, read only by emit, which publishes to_upload/1_variants_update.csv
    # from it as the cleaned subset of 1_variants_full.csv (one cleaning point; never uploaded itself).
    curate_delta_csv: Path = _OUTPUTS / "curate_delta.csv"
    # small CSV samples the test suite reads (self-contained in the package).
    tests_fixtures_dir: Path = REPO_ROOT / "tests" / "fixtures"

    # attribute name -> Medusa id; lives in from_medusa/<env>/ because its ids come FROM
    # that env's Medusa (like the variants export), not hand-maintained like catalog_source/.
    attributes_csv: Path = _FROM_MEDUSA / "attributes.csv"
    # which name field is a NAME and which an IDENTIFIER, published by the backend beside attributes.csv; the
    # one map, read only by reference/loaders/field_kinds.py and fetched by every produce (no committed copy).
    field_kinds_json: Path = _FROM_MEDUSA / "field-kinds.json"
    # The combined cross-category id-variants EXPORT (with Medusa Id), download-only:
    # the immutable "existing variants" the catalog reads. The UPLOAD files are PRODUCED
    # by the catalog (to_upload/) and never read back -- input and output never alias.
    variants_export_csv: Path = _FROM_MEDUSA / "variants_export.csv"
    # The committed, Id-free SOURCE OF TRUTH: the complete variant set (base == the full catalog). The
    # catalog seeds from THIS, not the live export -- so a cold start (Medusa emptied -> thin live export)
    # still rebuilds the whole catalog and only grows from there. sync_variants_base keeps it == full.
    variants_export_base_csv: Path = _FROM_MEDUSA / VARIANTS_BASE_FILE
    # The deliverable copy of the base this produce settled (catalog, after sync_variants_base), byte for byte, so
    # the manifest lists it beside 1_variants_full.csv and Blokport's "restore base" import can verify it.
    variants_base_deliverable_csv: Path = _TO_UPLOAD / VARIANTS_BASE_FILE
    # PRISTINE, read-only copy of the base, baked at image build (Dockerfile) and never written at runtime
    # (the live base above self-mutates: base := 1_variants_full each produce). The factory reset reconciles
    # the ledger against THIS so a seed cleanup actually reaches the running ledger. Absent locally -> the
    # committed base itself is pristine, so lifecycle falls back to it.
    variants_export_base_seed_csv: Path = _FROM_MEDUSA / "variants_export_base.seed.csv"

    # the ONE variety backbone every category reads (the pack's backbone_file). A pack naming none has no seed, so there
    # is no path to read: asking for one raises, naming the pack, never a guessed file.
    @property
    def backbone_json(self) -> Path:
        from stone_pipeline.config.domain import active_pack
        pack = active_pack()
        if pack.backbone_file is None:
            raise FileNotFoundError(f"domain pack {pack.name!r} names no backbone_file: it ships no variety seed")
        return _catalog_source_file(pack.backbone_file)

    @property
    def type_density_csv(self) -> Path:
        # per-material density, PACK-NAMESPACED (stone stays flat) so each material's densities are separate. A pack
        # with no such file uses its own default_density (domain pack), never the stone file -- so wood never inherits
        # stone's 2700. Seeded from that env's Medusa type table.
        return _reference_file("type_density.csv")

    @property
    def standard_slab_area_csv(self) -> Path:
        # PACK-NAMESPACED like type_density; absent for a pack with no standard-area concept (the bundle-area
        # ladder then simply falls through), so it never reads stone's slab-area table under another pack.
        return _reference_file("standard_slab_area.csv")

    @property
    def synonyms_dir(self) -> Path:
        # spelling -> canonical value per vocab, PACK-NAMESPACED (stone stays flat): wood never reads stone's synonyms
        return _reference_file("synonyms")

    @property
    def backbone_additions_dir(self) -> Path:
        # the per-run new-variety backbone deltas, PACK-NAMESPACED (stone stays flat). Under this instance's
        # catalog_source_dir, so a Paths pointed at another catalog_source writes its deltas there too
        return _pack_namespaced(self.catalog_source_dir) / "backbone_additions"

    @property
    def backbone_additions_file(self) -> Path:
        # curate's per-run delta: one post per new (type, name) variety, whatever its categories (phase 1)
        return self.backbone_additions_dir / "varieties.json"

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
    # dimension plausibility (stages/derive): a value is plausible within [lo x floor_fraction, hi x
    # ceiling_multiple] of the pack's per-category range; outside it is flagged out of range, and a value
    # above the band whose /100 lands inside it was recorded in centimetres
    dimension_range_floor_fraction: float = 0.3
    dimension_range_ceiling_multiple: float = 3.0
    # bundle size from area division (stages/derive): total / per-slab area is accepted as the slab count only
    # when it lies in [bundle_ratio_min, bundle_ratio_max] and is within bundle_ratio_tolerance of an integer
    bundle_ratio_min: int = 1
    bundle_ratio_max: int = 60
    bundle_ratio_tolerance: float = 0.15
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
    # When a scrape reports that more than this fraction of its per-product DETAIL fetches failed (rate-limited), its
    # product absences are untrustworthy -- refuse to delist against it. Below the 30% delist cap so a moderately
    # rate-limited run (10-25% detail failures) that would otherwise silently discontinue held products is caught.
    # Scrapers with no detail fetch never report a ratio (no-op).
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

    # Category pcat ids derive from the CATEGORIES registry (config/categories.py, single source of truth).
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
    # (dev/ vs prod/) follows BLOKPORT_ENV; bucket/profile/dry-run each
    # have their own override.
    bucket: str = S3_BUCKET
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


@dataclass(frozen=True)
class ImageProcessingConfig:
    """The re-host of each newly scraped photo (local/s3 image modes; passthrough never downloads bytes): the photo is
    size-reduced (its long edge capped, never enlarged; CPU only) and re-encoded into improved/, and its raw original
    is kept under scraped/ (keep_scraped), the photo process's input. Off (the default) hosts the raw bytes at the
    content key as they came. Enable per deployment with SCRAPER_IMAGE_PROCESSING=true."""

    enabled: bool = _env_bool("BLOKPORT_IMAGE_PROCESSING", False)
    target_long_edge: int = 2048      # cap the hosted photo's long edge at this (INTER_AREA downscale)
    jpeg_quality: int = 85            # visually identical to 92 for photos at ~30-40% smaller files
    keep_scraped: bool = _env_bool("BLOKPORT_KEEP_SCRAPED", False)


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
    # A product is live only with a standardised photo (whatever has no good image is delisted until it is fixed, one by
    # one): without one it is served at stock 0 with reason 'no_photo' and photo_ready false on the product (Blokport
    # hides it) and comes back on the first produce after its photo is published. On in production; dev has standardised
    # only a test set.
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
    # How often one texture job reads and merges the best-model marker again when another job's write overtook its own
    # (refresh_images.mark_best_model). Each refusal means some other job's write landed, and the GPU pool runs two
    # texture jobs at once, so a few attempts cover every writer; past them the job reports itself partial.
    marker_write_attempts: int = 5
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
    # Base images per category derive from the CATEGORIES registry (config/categories.py; image_prompts
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


@dataclass(frozen=True)
class AutoTextureConfig:
    """After a produce queues NEW-variant textures the lean :core cannot generate (no fal_client/torch),
    auto-submit ONE on-demand GPU Batch job to generate + upload them (stone_pipeline.io.texture_job). Part of the
    produce (Republish), NOT a cron: it fires only when a produce actually queued textures. The job runs on the
    gpu_enhance module's queue and job definition (the :gpu image, RUN_MODE=generate-textures ->
    prepare_variant_images, the FLUX.2 -> BEN2 -> upload pipeline). Fire-and-forget; the next produce stamps the
    images (a one-cycle hold). Off by default (BLOKPORT_AUTO_TEXTURE)."""

    enabled: bool = _env_bool("BLOKPORT_AUTO_TEXTURE", False)
    queue: str = env.getenv("BLOKPORT_GPU_QUEUE", "")
    job_definition: str = env.getenv("BLOKPORT_GPU_JOBDEF", "")
    # Per-dispatch queue objects are ephemeral coordination, consumed by their GPU job within minutes. The publish
    # deletes any older than this so the prefix self-heals instead of accumulating forever. Far above a job's runtime
    # (jobs finish in minutes; the Batch attempt ceiling is hours), so it can never delete a queue a running or
    # just-published job still needs.
    queue_ttl_seconds: int = 86400                     # 1 day
    # Per-image-script wall-clock ceiling. Generation (FLUX over the whole queue) legitimately runs minutes; this is a
    # backstop far above normal so a wedged stage (a hung model load or network call) fails and flags its variants
    # HELD instead of pinning the whole Batch task.
    script_timeout_s: int = 3600


@dataclass(frozen=True)
class VarietyColourConfig:
    """Cut-offs of the product-photo colour classifier (stages/variety_color.classify): median stone RGB ->
    a Medusa colour. Saturation and value on 0..1, hue in degrees, warm_margin in 0..255 red-over-blue."""

    warm_margin: int = 12                 # red exceeds blue by more than this -> a warm light stone (Cream)
    # the greyscale family: pure neutral (saturation below neutral_saturation), or a washed-out LIGHT stone
    # (saturation below washed_saturation and value above washed_value)
    neutral_saturation: float = 0.10
    washed_saturation: float = 0.25
    washed_value: float = 0.80
    black_value: float = 0.27             # greyscale value below this -> Black
    grey_value: float = 0.70              # below this -> Grey; above -> White / Cream (warm)
    # hue buckets, upper bound exclusive: red wraps around 0 (below red_hue or from red_wrap_hue)
    red_hue: float = 20.0
    red_wrap_hue: float = 345.0
    orange_hue: float = 45.0              # Brown (value below brown_value), else Gold (saturation above
    brown_value: float = 0.55             # gold_saturation) or Beige
    gold_saturation: float = 0.4
    yellow_hue: float = 70.0              # Yellow when value above yellow_value, else Brown
    yellow_value: float = 0.6
    green_hue: float = 170.0
    blue_hue: float = 255.0
    purple_hue: float = 290.0             # above: Pink


@dataclass(frozen=True)
class LotConfig:
    """A lot is a set of ordinary products (pieces) sharing one lot_code (stages/lots.py). The code is
    `<label>-<uuid5(code_namespace, "<source_code>:<lot ref>")>`: unique, byte-identical on every piece,
    stable across runs. The namespace is fixed for good: changing it re-keys every lot on the next produce,
    so Blokport would see each as a new lot (guarded by a test)."""

    code_namespace: str = "be1ef5d0-ee2e-4157-b6ec-8870c49e7deb"
    max_code_length: int = 128   # Blokport's stated limit for lot_code (additional_data text field)


@dataclass(frozen=True)
class SyncConfig:
    """The sync ledger's serve/ack tunables (ledger/sync.py, ledger/server.py: SYNC_LEDGER_DESIGN.md section 8)."""

    # After this many CONSECUTIVE failed applies (or refused acks) an entity is dead-lettered: it stops being served
    # (no infinite poison-pill retry) and surfaces in GET /sync/v1/failures for a human. Every lane uses it.
    max_attempts: int = 5
    # A failed apply is not re-served on the very next pull: a passing hiccup (an S3 blip while Medusa copies a photo)
    # would fail every immediate retry too and reach the cap within seconds. It waits retry_base_seconds, multiplied
    # by retry_factor with each consecutive failure (5, 10, 20, 40 min), so the cap means over an hour of failures.
    retry_base_seconds: int = 300
    retry_factor: int = 2
    # A served-but-not-yet-acked entity is leased in state 'syncing'; an un-acked lease is reclaimed to 'dirty' after
    # this many seconds (comfortably longer than a pull -> apply -> ack cycle) and re-served.
    syncing_lease_seconds: int = 900
    # An authed request body cap, so a hostile Content-Length cannot force an unbounded read. The largest real body
    # is an ack page (2000 items, well under 1 MiB).
    max_body_bytes: int = 8 << 20
    # Page bounds for the pull routes: an unbounded pull on a full delta builds it all in memory and writes it in one
    # buffer (http.server has no backpressure); a 24k-item bootstrap delta is ~4.6 MB.
    default_page: int = 500
    max_page: int = 2000
    default_failures_page: int = 200


@dataclass(frozen=True)
class RunConfig:
    """How a produce/build/clean run is scoped, read ONCE from the env (project rule: config-driven, no
    argparse). SCRAPER_ prefix, legacy BLOKPORT_ also read (SCRAPER_ wins). The `__main__` entry of
    stone_pipeline.build / .produce / .clean reads these; the core functions take the same values as
    explicit arguments. The config server's runner sets them on the produce subprocess's env."""
    # how far a produce/build goes: one of build.STAGES (scrape|catalog|republish|inventory|all). SCRAPER_RUN_STAGE.
    stage: str = (env.getenv("BLOKPORT_RUN_STAGE", "all") or "all").strip().lower()
    # which scrapers; empty -> every enabled source (as a scoped list omitted did). SCRAPER_RUN_SOURCES (comma-separated).
    sources: tuple[str, ...] = tuple(s.strip() for s in (env.getenv("BLOKPORT_RUN_SOURCES", "") or "").split(",") if s.strip())
    # verify-only: re-run the consistency gate on the existing files, ignoring stage/sources. SCRAPER_RUN_VERIFY.
    verify: bool = _env_bool("BLOKPORT_RUN_VERIFY", False)
    # housekeeping (stone_pipeline.clean): report only, remove nothing. SCRAPER_CLEAN_DRY_RUN.
    clean_dry_run: bool = _env_bool("BLOKPORT_CLEAN_DRY_RUN", False)


@dataclass(frozen=True)
class Settings:
    environment: str = BLOKPORT_ENV  # "development" | "production" (BLOKPORT_ENV)
    code_version: str = "0.1.0"

    paths: Paths = field(default_factory=Paths)
    thresholds: Thresholds = field(default_factory=Thresholds)
    admission: Admission = field(default_factory=Admission)
    backend: BackendConstants = field(default_factory=BackendConstants)
    s3: S3Config = field(default_factory=S3Config)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    auto_texture: AutoTextureConfig = field(default_factory=AutoTextureConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    curation: CurationConfig = field(default_factory=CurationConfig)
    lots: LotConfig = field(default_factory=LotConfig)
    variety_colour: VarietyColourConfig = field(default_factory=VarietyColourConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    run: RunConfig = field(default_factory=RunConfig)

    # The source proven first (section 14). develi is absent from the supplied
    # data, so polonine is the clean named-variety proving source.
    spine_source: str = "polonine"


SETTINGS = Settings()
