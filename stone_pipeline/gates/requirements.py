"""The single Medusa requirement bar -- the documented source of truth for what a canonical row must
satisfy to emit as a Medusa product.

There is ONE destination (Medusa, its current data model), so there is ONE set of HARD requirements.
`stages/validate.py` (Stage 9) is the SINGLE enforcement authority -- no gate hard-rejects remain (the
process gate's origin check became a soft diagnostic when its hard reject moved into validate). This module
does NOT re-implement or change that enforcement; it PINS it: `tests/test_requirements_registry.py` asserts
this list stays in lockstep with the rules validate can reject with, so a hard requirement can never
silently appear or vanish as the pipeline evolves.

Input variety is handled elsewhere: different producers (some ERP-fed, some scraped) are onboarded by
SOURCE-ISOLATION -- a source is a fetcher + adapter + per-source contract -- NOT here. Every source, however
it is fed, is validated against this ONE bar; `satisfied_by` records how a source provides or derives each
field (and, when it cannot, the row is held for review rather than shipped wrong). See GATE_REQUIREMENTS.md
for the full seam-by-seam blueprint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Requirement:
    rule: str          # the RejectReason.rule validate emits when unmet -- the machine-checked key
    needs: str         # the canonical field(s) / condition a row must satisfy to pass
    satisfied_by: str  # how a source provides or derives it (else the row is held, never shipped wrong)


# Keep in lockstep with stone_pipeline/stages/validate.py. The guard test enforces that this set EXACTLY
# equals the rules validate can reject with -- add/remove here whenever a hard requirement changes there.
MEDUSA_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("required_id_null", "type_id, color_id, finish_id, quality_id, variation_id all non-null",
                "normalize resolves ids; colour fills from the variety, finish/quality from last-resort defaults"),
    Requirement("tree_gap", "no unresolved tree gap (variation + leaf resolved)",
                "match_variation + reconcile_tree; unresolved -> the review queue"),
    Requirement("category_invalid", "category_pcat_id is an ACTIVE category's pcat",
                "derive_category from the resolved format; an inactive category (e.g. tiles until set) is held"),
    Requirement("handle_missing", "handle and slug non-empty",
                "derive_handle from variation_name/raw_name + source_code + surrogate_key"),
    Requirement("handle_collision", "handle globally unique",
                "surrogate_key uniqueness (Stage 2 keys/dedupe)"),
    Requirement("owner_missing", "company_id and sales_channel_id non-empty",
                "constants (Stage 8) from config; prod must set the env vars"),
    Requirement("origin_missing", "origin_country_code non-empty",
                "scrape origin -> origin_map -> supplier default; none -> held"),
    Requirement("no_image", "an image present when images are required (unless no_publishable_image)",
                "raw_image_urls -> the image stage; a terminal non-stone set publishes imageless"),
    Requirement("dimension_unavailable", "no dimension whose source FETCH failed (transient)",
                "held for retry next scrape; never defaulted (freight-critical)"),
    Requirement("dimension_defaulted", "no genuinely-absent dimension filled from the pack default",
                "held for review so a real size is supplied before it sells"),
    Requirement("dimension_invalid", "length, width, height all > 0",
                "parsed dims or pack defaults; a parsed 0 is a data error and is held"),
    Requirement("stock_undetermined", "inventory_quantity determined (a real value, including 0)",
                "a count, or a piece-count derived from an available stock area; else held"),
)

MEDUSA_RULES: frozenset[str] = frozenset(r.rule for r in MEDUSA_REQUIREMENTS)
