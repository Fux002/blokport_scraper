"""The module-boundary contracts, one per logical pipeline module.

Phase A wires INGEST and PROCESS. CLEAN / IMAGES / UPGRADE are declared as the
plan's remaining boundaries and wired in later phases.
"""

from __future__ import annotations

from typing import Callable

from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.gates.contract import SOFT, Invariant, ModuleContract
from stone_pipeline.reference.loaders import ReferenceData


# --- ingest: the post-scrape check ----------------------------------------------------------------
# After adapt, every row should carry the raw fields its downstream module needs. These are
# report-only (they feed the gate status + the "what's missing" diagnostics) so the scrape's
# completeness is visible immediately, without changing which rows emit. A field missing across
# most of the batch escalates the gate -- a systemically incomplete scrape, caught right after the
# scraper instead of at the Medusa upload screen.
def _has_variety_name(row: CanonicalRow) -> bool:
    return bool((row.raw_name or "").strip() or (row.variety_match_key or "").strip())


def _has_dimensions_source(row: CanonicalRow) -> bool:
    return bool((row.raw_dimensions or "").strip() or (row.raw_thickness or "").strip()
                or (row.raw_total_m2 or "").strip() or (row.raw_per_slab_m2 or "").strip())


def _has_image_source(row: CanonicalRow) -> bool:
    return bool(row.raw_image_urls)


INGEST = ModuleContract(
    module="ingest",
    invariants=(
        Invariant("ingest_missing_variety_name", _has_variety_name, severity=SOFT, field="raw_name"),
        Invariant("ingest_missing_dimensions_source", _has_dimensions_source, severity=SOFT,
                  field="dimensions"),
        Invariant("ingest_missing_image_source", _has_image_source, severity=SOFT, field="image_urls"),
    ),
)


# --- clean: the normalized-and-reconciled contract ------------------------------------------------
# After the clean module (dedupe -> format -> normalize -> match -> reconcile), each resolved
# attribute name must be the CANONICAL spelling for its id, not a raw source token. This is the
# safety net for the casing-leak bug class (a miscased 'QUARTZITE' that still carries the right id
# but the wrong display name): root-caused in normalize, guarded here so it can never silently
# return. Soft (flag, never reject) -- the id is valid, only the display string is off. The
# tree-gap invariant is report-only: it gives boundary visibility into how many rows failed to
# resolve a variation/leaf (validate still hard-rejects them downstream).
_CASING_ATTRS = (("type", "type_name"), ("color", "color_name"),
                 ("finish", "finish_name"), ("quality", "quality_name"))


def _canonical_casing(ref: ReferenceData, category: str, field: str) -> Callable[[CanonicalRow], bool]:
    def _check(row: CanonicalRow) -> bool:
        name = getattr(row, field, None)
        if not name:
            return True  # absent -> required-ness is the Process/validate concern, not casing
        looked = ref.attributes.resolve_id(category, name)
        # Satisfied unless the value resolves to a DIFFERENT spelling (i.e. it's miscased).
        # An unresolvable value (looked is None) is a separate problem already flagged upstream.
        return looked is None or looked[0] == name

    return _check


def clean_contract(ref: ReferenceData) -> ModuleContract:
    """Built with `ref` because canonical casing is defined by the attribute vocabulary."""
    casing = tuple(
        Invariant(f"clean_noncanonical_{field}", _canonical_casing(ref, category, field),
                  severity=SOFT, flag_code=FlagCode.noncanonical_attr_casing, field=field)
        for category, field in _CASING_ATTRS
    )
    tree_gap = Invariant("clean_unresolved_tree_gap", lambda r: not r.tree_gaps,
                         severity=SOFT, field="variation")  # report-only (no flag_code)
    return ModuleContract(module="clean", invariants=(*casing, tree_gap))


# --- process: the Medusa-importable output contract -----------------------------------------------
# After derive + constants, a row must carry an origin country code: Medusa requires it for its
# pricing-rule lookup, so a blank breaks the import. derive_origin now always fills it when the
# source has an origin_default; a row that still lacks one (no scrape origin, no origin_map entry,
# no supplier default) is rejected here with a precise reason rather than emitted broken.
PROCESS = ModuleContract(
    module="process",
    required_fields=("origin_country_code",),
)
