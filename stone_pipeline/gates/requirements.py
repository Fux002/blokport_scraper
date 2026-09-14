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
    # operator-facing worklist (the diagnostics "skipped rows" panel; see run._held_breakdown):
    title: str = ""    # one short line: what a hold on this rule means
    kind: str = "decision"  # how it clears: 'decision' (operator acts), 'transient' (self-heals next scrape),
                            # 'config' (an environment/system misconfiguration, not the row's data)
    recovery: str = ""      # what the operator does, or that it re-lists on its own


# Keep in lockstep with stone_pipeline/stages/validate.py. The guard test enforces that this set EXACTLY
# equals the rules validate can reject with -- add/remove here whenever a hard requirement changes there.
MEDUSA_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("required_id_null", "type_id, color_id, finish_id, quality_id, variation_id all non-null",
                "normalize resolves ids; colour fills from the variety, finish/quality from last-resort defaults",
                title="Attribute or variety not in the catalogue yet", kind="decision",
                recovery="Add the missing colour/finish/quality value in Medusa and paste its id, or mint the "
                         "variety, in review. Lists on the next produce."),
    Requirement("tree_gap", "no unresolved tree gap (variation + leaf resolved)",
                "match_variation + reconcile_tree; unresolved -> the review queue",
                title="New or uncertain variety, or an attribute not yet allowed for it", kind="decision",
                recovery="Resolve it in the variety / leaf review queue (mint the variety, confirm the match, "
                         "or approve the attribute for it). Lists on the next produce."),
    Requirement("category_invalid", "category_pcat_id is an ACTIVE category's pcat",
                "derive_category from the resolved format; an inactive category (e.g. tiles until set) is held",
                title="Selling format's category is not active yet", kind="decision",
                recovery="Activate the category (set its Medusa pcat id). Lists once the category is live."),
    Requirement("handle_missing", "handle and slug non-empty",
                "derive_handle from variation_name/raw_name + source_code + surrogate_key",
                title="Product handle could not be built", kind="config",
                recovery="A system issue in handle derivation, not the row's data. Report it."),
    Requirement("handle_collision", "handle globally unique",
                "surrogate_key uniqueness (Stage 2 keys/dedupe)",
                title="Two products resolved to the same handle", kind="config",
                recovery="A system issue (surrogate collision), not the row's data. Report it."),
    Requirement("owner_missing", "company_id and sales_channel_id non-empty",
                "constants (Stage 8) from config; prod must set the env vars",
                title="Company / sales channel not configured", kind="config",
                recovery="Set the environment's company and sales-channel ids. Not a data problem."),
    Requirement("origin_missing", "origin_country_code non-empty",
                "scrape origin -> origin_map -> supplier default; none -> held",
                title="Origin country could not be resolved", kind="decision",
                recovery="Set the variety's origin or the supplier's default origin. Lists on the next produce."),
    Requirement("no_image", "an image present when images are required (unless no_publishable_image)",
                "raw_image_urls -> the image stage; a terminal non-stone set publishes imageless",
                title="No usable photo this run", kind="transient",
                recovery="Held for retry; re-lists automatically once an image is scraped."),
    Requirement("dimension_unavailable", "no dimension whose source FETCH failed (transient)",
                "held for retry next scrape; never defaulted (freight-critical)",
                title="Size fetch failed (temporary)", kind="transient",
                recovery="Held for retry; re-lists automatically on the next scrape."),
    Requirement("dimension_invalid", "length, width, height all > 0",
                "parsed dims or pack defaults; a parsed 0 is a data error and is held",
                title="Unreadable size (zero or malformed)", kind="transient",
                recovery="Re-lists on the next scrape that carries a valid size; or fix the source's size parse."),
    Requirement("stock_undetermined", "inventory_quantity determined (a real value, including 0)",
                "a count, or a piece-count derived from an available stock area; else held",
                title="Stock could not be determined", kind="decision",
                recovery="Fix the scrape/adapter to read the stock, or set it. Lists on the next produce."),
)

MEDUSA_RULES: frozenset[str] = frozenset(r.rule for r in MEDUSA_REQUIREMENTS)
