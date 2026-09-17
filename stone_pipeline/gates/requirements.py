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
    stage: str = ""    # the diagnostics LAYER that detects this (Validate rejects, but the problem is found
                       # upstream): so the "skipped" panel says WHERE, using the exact layer labels the table
                       # shows (Normalize, Match Variation, Reconcile, Derive, Images, Keys Dedupe, Constants)


@dataclass(frozen=True)
class WorklistRefinement:
    """ONE hard rule can hold a row for reasons the operator clears differently. A refinement gives such a
    case its own worklist entry, keyed on the rule, the reject detail and whether the row is bound to a
    variety; it carries its own text. It is NOT a reject rule (validate still emits the hard rule), so the
    lockstep guard on MEDUSA_REQUIREMENTS is untouched."""
    key: str           # the worklist entry's key (what the diagnostics panel groups by)
    rule: str          # the hard rule the row was rejected with
    detail: str        # the reject detail that selects this case
    bound: bool | None # True: only a row bound to a variety; False: only an unbound row; None: either
    title: str
    recovery: str
    kind: str = "decision"
    stage: str = ""    # the diagnostics layer that detects this case (see Requirement.stage)


# A variety minted from a colourless listing has no colour for its colourless listings to inherit, and no
# review card is raised (the listing proposes nothing). The generic required_id_null text sent the operator
# to Medusa; the fix is the amend flow, since any statement's colour documents the variety's colour.
WORKLIST_REFINEMENTS: tuple[WorklistRefinement, ...] = (
    WorklistRefinement("variety_no_colour", "required_id_null", "color_id", bound=True,
                       title="Variety has no documented colour",
                       recovery="Amend the variety in Review and set a colour (any statement's colour documents "
                                "it), then Republish. Lists on the next produce.", stage="Normalize"),
    # A no_image reject with detail "no_source" is a listing the supplier shows no photo for: nothing failed
    # and nothing can be retried; it lists only once the supplier adds one. The generic no_image entry is a
    # photo that failed to fetch or process this run, which the next scrape retries on its own.
    WorklistRefinement("supplier_no_photo", "no_image", "no_source", bound=None,
                       title="Supplier lists no photo for this product", kind="transient",
                       recovery="Nothing failed and nothing to decide: the listing carries no image on the "
                                "supplier's site. Lists once the supplier adds a photo and the next scrape "
                                "picks it up.", stage="Images"),
)


# Keep in lockstep with stone_pipeline/stages/validate.py. The guard test enforces that this set EXACTLY
# equals the rules validate can reject with -- add/remove here whenever a hard requirement changes there.
MEDUSA_REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement("required_id_null", "type_id, color_id, finish_id, quality_id, variation_id all non-null",
                "normalize resolves ids; colour fills from the variety, finish/quality from last-resort defaults",
                title="Attribute or variety has no Medusa id yet", kind="decision",
                recovery="A colour, finish, quality or variety this product needs does not exist in Medusa yet. "
                         "Add the value in Medusa (the next produce picks up the export) or mint the "
                         "variety in Review. Lists on the next produce.", stage="Normalize / Match Variation"),
    Requirement("tree_gap", "no unresolved tree gap (variation + leaf resolved)",
                "match_variation + reconcile_tree; unresolved -> the review queue",
                title="Variety unresolved, or an attribute not yet allowed for it", kind="decision",
                recovery="Decide it in Review: bind or mint the variety, or approve the attribute for it. "
                         "Lists on the next produce.", stage="Match Variation / Reconcile"),
    Requirement("category_invalid", "category_pcat_id is an ACTIVE category's pcat",
                "derive_category from the resolved format; an inactive category (e.g. tiles until set) is held",
                title="Selling format's category is not active", kind="decision",
                recovery="Activate the category by setting its Medusa category id. Lists once it is active.", stage="Derive"),
    Requirement("handle_missing", "handle and slug non-empty",
                "derive_handle from variation_name/raw_name + source_code + surrogate_key",
                title="Product handle could not be built", kind="config",
                recovery="A pipeline defect in handle derivation, not the product's data. Report it.", stage="Derive"),
    Requirement("handle_collision", "handle globally unique",
                "surrogate_key uniqueness (Stage 2 keys/dedupe)",
                title="Two products resolved to the same handle", kind="config",
                recovery="A pipeline defect (key collision), not the product's data. Report it.", stage="Keys Dedupe"),
    Requirement("owner_missing", "company_id and sales_channel_id non-empty",
                "constants (Stage 8) from config; prod must set the env vars",
                title="Company or sales channel not configured", kind="config",
                recovery="Set the environment's company and sales channel ids. Not a data problem.", stage="Constants"),
    Requirement("origin_missing", "origin_country_code non-empty",
                "scrape origin -> origin_map -> supplier default; none -> held",
                title="Origin country could not be resolved", kind="decision",
                recovery="Confirm the origin card in Review, set the variety's origin, or set the supplier's "
                         "default origin. Lists on the next produce.", stage="Derive"),
    Requirement("no_image", "an image present when images are required (unless no_publishable_image)",
                "raw_image_urls -> the image stage; a terminal non-stone set publishes imageless",
                title="Photo failed to fetch or process this run", kind="transient",
                recovery="Retried on the next scrape; lists once the photo comes through.", stage="Images"),
    Requirement("dimension_unavailable", "no dimension whose source FETCH failed (transient)",
                "held for retry next scrape; never defaulted (freight-critical)",
                title="Size fetch failed this run", kind="transient",
                recovery="Retried on the next scrape; lists once the size comes through.", stage="Derive"),
    Requirement("dimension_invalid", "length, width, height all > 0",
                "parsed dims or pack defaults; a parsed 0 is a data error and is held",
                title="Unreadable size (zero or malformed)", kind="transient",
                recovery="Lists on the next scrape that carries a valid size. If it persists, the source's size "
                         "parse needs a fix.", stage="Derive"),
    Requirement("stock_undetermined", "inventory_quantity determined (a real value, including 0)",
                "a count, or a piece-count derived from an available stock area; else held",
                title="Stock could not be determined", kind="decision",
                recovery="The scrape carries no stock signal. Fix the adapter to read it, or set the stock. "
                         "Lists on the next produce.", stage="Derive"),
)

MEDUSA_RULES: frozenset[str] = frozenset(r.rule for r in MEDUSA_REQUIREMENTS)
