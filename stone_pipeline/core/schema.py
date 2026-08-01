"""Canonical staging schema and the small value types shared across stages.

Section 4 (canonical staging schema), section 5 (Resolution), section 9.1
(review flag structure). Pydantic enforces the shapes in code so a malformed
row is caught at the boundary, not three stages later.

The canonical table is stored as parquet between stages (section 13A.1). This
module also exposes the polars dtype map so the table shape is declared once.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from stone_pipeline.config.settings import Confidence


# --- Review flag codes: a closed enum (section 9.1) ---------------------------
class FlagCode(str, Enum):
    attr_unresolved = "attr_unresolved"
    variation_review = "variation_review"
    leaf_snapped = "leaf_snapped"
    bundle_default = "bundle_default"
    bundle_estimated = "bundle_estimated"
    bundle_ratio_noninteger = "bundle_ratio_noninteger"
    stock_unparseable = "stock_unparseable"        # stock field present but unparseable -> shipped out-of-stock (0)
    origin_unresolved = "origin_unresolved"
    origin_supplier_default = "origin_supplier_default"
    origin_multi_home_absent = "origin_multi_home_absent"
    # the curated (name, type) origin lookups were SKIPPED because the row's type is not
    # variation-authoritative (name-derived / fallback), so a homonym could not resolve the wrong stone's
    # origin; the row fell to the supplier default and needs its variety type verified.
    origin_type_unverified = "origin_type_unverified"
    image_placeholder = "image_placeholder"
    image_download_failed = "image_download_failed"
    near_duplicate = "near_duplicate"
    format_inferred = "format_inferred"
    format_unresolved = "format_unresolved"
    dimension_out_of_range = "dimension_out_of_range"
    # a dimension stored in the wrong unit (centimetres read as metres, ~100x too large) was corrected to
    # metres against the category's physical range, before weight -- flagged so the source can be verified.
    dimension_unit_corrected = "dimension_unit_corrected"
    dimension_defaulted = "dimension_defaulted"    # a genuinely-absent dim filled from the pack's dimension_defaults
    # transient: a required dimension's FETCH failed (recoverable, e.g. HTTP 429) -> the row is HELD for
    # retry, NOT defaulted. The twin of no_image, the deliberate counterpart to dimension_defaulted.
    dimension_unavailable = "dimension_unavailable"
    weight_derived = "weight_derived"
    attr_last_resort = "attr_last_resort"
    multi_value = "multi_value"
    type_overridden_by_variety = "type_overridden_by_variety"
    attr_from_variety = "attr_from_variety"
    noncanonical_attr_casing = "noncanonical_attr_casing"
    no_image = "no_image"                          # transient: nothing scraped / download failed -> retry
    # terminal: images WERE scraped but Stage 7 (the image pipeline) classified every one as non-stone
    # (spec sheet, pure vendor logo) and discarded them. A known-good empty, NOT a failure: the product is a
    # real stone with no usable photo. It publishes without an image; the discard reason (provenance) is
    # carried on this flag for review; it is never retried/alarmed as broken. Emitted ONLY by Stage 7.
    no_publishable_image = "no_publishable_image"
    ports_default = "ports_default"
    surrogate_minted = "surrogate_minted"
    degraded_batch = "degraded_batch"


class GapKind(str, Enum):
    missing_variation = "missing_variation"
    missing_leaf_child = "missing_leaf_child"
    missing_attribute = "missing_attribute"
    # the row's format (tile) has no variation reference loaded yet; the tile_
    # variants export must be supplied before these can resolve
    missing_tile_reference = "missing_tile_reference"
    # the row's category does not share the stone-variety vocabulary (e.g.
    # accessories); its own matching vertical is not built yet, so it is held
    # rather than mismatched to a stone variety.
    unsupported_category = "unsupported_category"


# --- The Resolution: a single strategy result (section 5) ---------------------
class Resolution(BaseModel):
    """The output of one Strategy. value, confidence, method, evidence."""

    value: Any = None
    confidence: Confidence = Confidence.none
    method: str = "no_strategy"
    evidence: dict[str, Any] = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class ReviewFlag(BaseModel):
    """Structured, filterable review flag (section 9.1)."""

    field: str
    code: FlagCode
    raw_value: Optional[str] = None
    best_guess: Optional[str] = None
    confidence: Confidence = Confidence.none
    method: str = ""
    src_url: Optional[str] = None


class RejectReason(BaseModel):
    """A single hard failure that prevents a row from emitting (section 9, Stage 9)."""

    rule: str
    detail: str = ""


class TreeGap(BaseModel):
    """One distinct missing tree entity (section 8.2). De-duplicated per batch."""

    src_site: str
    surrogate_key: str
    raw_name: str = ""
    normalized_name: str = ""
    suggested_type: Optional[str] = None
    suggested_color: Optional[str] = None
    suggested_finish: Optional[str] = None
    suggested_quality: Optional[str] = None
    gap_kind: GapKind
    nearest_existing: Optional[str] = None
    nearest_score: Optional[float] = None
    example_src_url: Optional[str] = None


# --- The canonical row (section 4) --------------------------------------------
class CanonicalRow(BaseModel):
    """One cleaned product. Adapters fill src_ and raw_; stages fill the rest.

    Every matched or derived field has a sibling _confidence and _method (stored
    as plain strings/floats so they survive parquet round-trips cleanly).
    """

    # provenance / keys
    src_site: str
    src_natural_key: Optional[str] = None
    surrogate_key: Optional[str] = None
    src_url: Optional[str] = None
    scrape_timestamp: Optional[str] = None
    adapter_version: Optional[str] = None

    # raw scraped (set by adapter)
    raw_name: Optional[str] = None
    variety_match_key: Optional[str] = None
    raw_type: Optional[str] = None
    raw_color: Optional[str] = None
    raw_finish: Optional[str] = None
    raw_quality: Optional[str] = None
    raw_format: Optional[str] = None
    raw_origin: Optional[str] = None
    raw_thickness: Optional[str] = None
    raw_dimensions: Optional[str] = None
    raw_weight: Optional[str] = None
    raw_total_m2: Optional[str] = None
    raw_per_slab_m2: Optional[str] = None
    raw_slab_count: Optional[str] = None
    raw_bundle_size: Optional[str] = None
    raw_slabs_array: Optional[str] = None
    raw_image_urls: list[str] = Field(default_factory=list)
    raw_description: Optional[str] = None
    # Field-groups whose sub-fetch FAILED for this product (from the scraper's reserved `fetch_failed`
    # column, e.g. ["dims"]). A failed-fetch value is recoverable, so derive HOLDS it (never defaults a
    # fabricated value) and the row retries next scrape. Deliberately NOT `src_/raw_`-prefixed, so it is
    # invisible to the adapter fixture compare (adapters/selftest.row_to_comparable).
    fetch_failed_fields: list[str] = Field(default_factory=list)
    raw_inventory_quantity: Optional[str] = None

    # resolved attributes (Stage 3 / Stage 5)
    type_id: Optional[str] = None
    type_name: Optional[str] = None
    type_confidence: str = "none"
    type_method: str = ""
    color_id: Optional[str] = None
    color_name: Optional[str] = None
    color_confidence: str = "none"
    color_method: str = ""
    finish_id: Optional[str] = None
    finish_name: Optional[str] = None
    finish_confidence: str = "none"
    finish_method: str = ""
    quality_id: Optional[str] = None
    quality_name: Optional[str] = None
    quality_confidence: str = "none"
    quality_method: str = ""

    # variation (Stage 4)
    variation_id: Optional[str] = None       # the matched variety's Medusa id (from the export)
    variation_key: Optional[str] = None      # the matched variety's STABLE Key -- the durable link
    variation_name: Optional[str] = None
    variation_confidence: str = "none"
    variation_method: str = ""

    # derived (Stage 6)
    category_pcat_id: Optional[str] = None
    category_method: str = ""
    format_value: Optional[str] = None
    format_method: str = ""
    format_confidence: str = "none"
    is_block: bool = False
    sold_in_bundle: Optional[bool] = None
    bundle_size: Optional[int] = None
    bundle_size_confidence: str = "none"
    bundle_size_method: str = ""
    # Stock level (units available), derived ONCE in Stage 6 -- SEPARATE from bundle_size (the
    # slabs-per-bundle multiplier). Every writer reads this field; nothing recomputes stock at write time.
    inventory_quantity: Optional[int] = None
    inventory_confidence: str = "none"
    inventory_method: str = ""
    weight: Optional[float] = None
    length: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None
    dimension_method: str = ""
    origin_country_code: Optional[str] = None
    origin_city: Optional[str] = None
    origin_county: Optional[str] = None
    origin_confidence: str = "none"
    origin_source: str = ""
    port_ids: list[str] = Field(default_factory=list)

    # generated (Stage 6)
    title: Optional[str] = None
    title_method: str = ""
    description: Optional[str] = None
    description_method: str = ""
    handle: Optional[str] = None
    slug: Optional[str] = None

    # image stage (Stage 7)
    image_keys: list[str] = Field(default_factory=list)
    thumbnail_key: Optional[str] = None
    oriented_image_keys: list[str] = Field(default_factory=list)
    product_image_keys: list[str] = Field(default_factory=list)

    # config constants (Stage 8)
    company_id: Optional[str] = None
    sales_channel_id: Optional[str] = None
    visibility: Optional[str] = None
    discountable: Optional[str] = None

    # system
    review_flags: list[ReviewFlag] = Field(default_factory=list)
    reject_reasons: list[RejectReason] = Field(default_factory=list)
    tree_gaps: list[TreeGap] = Field(default_factory=list)
    row_fingerprint: Optional[str] = None
    degraded: bool = False
    # whether this product already exists in Medusa (by SKU) and whether it changed
    product_status: str = ""   # "new" | "existing" | ""
    product_changed: Optional[bool] = None

    model_config = {"use_enum_values": True}

    # --- convenience -----------------------------------------------------------
    def add_flag(self, flag: ReviewFlag) -> None:
        self.review_flags.append(flag)

    def add_reject(self, reason: RejectReason) -> None:
        self.reject_reasons.append(reason)

    def add_gap(self, gap: TreeGap) -> None:
        self.tree_gaps.append(gap)

    @property
    def is_emittable(self) -> bool:
        return len(self.reject_reasons) == 0
