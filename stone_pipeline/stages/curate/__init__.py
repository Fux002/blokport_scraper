"""Variant and alias curation (the manual-update loop, section 8.3 / 8.4, targeting
the Medusa IMPORT files).

A run surfaces two things the reference is missing:
  - forgotten aliases: a scraped spelling that resolved only via a non-exact tier
    (fuzzy/phonetic) or sits in the review band; it should become an alias of the
    matched variety so next time it is a clean exact hit.
  - new variants: a scraped name that matched nothing; after an alias-vs-new
    decision it may be a genuinely new variety to create.

The system emits additions in the EXACT import-file format (Key,Name,Image,
Aliases) so they can be loaded into Medusa to create/update variants, which then
assigns ids and re-exports the id files the pipeline matches against. It never
rewrites the supplied files; it writes separate curation files for review.

Because a stone is the same material as a slab, block, or tile (only the id and
Key prefix differ), an addition is emitted for every category's import file.
Keys match the existing convention exactly: {branch}_{type}_{name}_{uuid}, where
the uuid is a deterministic uuid5 (stable across runs, structurally a valid uuid).

THE CLEANING / VERIFICATION FLOW spans several stages, each doing its cleaning at the
point in the pipeline where the needed information first exists -- so the order is:
  1. adapter   strip supplier codes + format words from the raw name (clean_variety_name)
  2. normalize resolve type/colour/finish/quality; NAME-over-tag corrects a mis-tagged
               type ('Azul White Quartzite' tagged Onyx -> Quartzite) BEFORE matching, so
               the variety is matched + minted under the correct type
  3. loaders   drop JUNK existing variants (bare-code 'Mgt'; mis-typed under the wrong Key
               type) from the match set, so products never bind to them (they're listed in
               variants_to_delete.csv)
  4. matching  resolve to an existing variant (exact -> projection -> fuzzy -> phonetic)
  5. curate    classify what's still unresolved, in STRICT priority order:
               CANONICALISE -> DEDUP -> REJECT/HOLD junk -> RESOLVE to existing -> MINT new.
The single rule for every gate's position: do the cheapest, most DECISIVE thing first, and
REUSE before MINT; when uncertain, HOLD for human review rather than guess.

The package, one module per responsibility (each phase reads and writes the shared Curation context):
  keys        deterministic Keys, branches, image names
  existing    the existing-variety index (one read of the export/base), per (name, type)
  context     the shared context, the result, the ONE identity derivation, the decision lookup
  review      review cards, scraped evidence, the HOLD outcomes
  aliasing    phase 1 (matched-row aliases) and phase 3 (alias additions), alias routing
  classify    phase 2: classify each gapped variety in strict priority order
  minting     phase 4 (new variants: import row, backbone post, image) and phase 5 (leaf suggestions)
  attributes  the attribute (colour/finish/type/quality) curation
  output      the written outputs and operator queues
  pipeline    build_curation and the stage entry `run`

This facade is the package's public surface and its operator/test seam: `_alias_model` is resolved
through it at call time, so replacing it here replaces the model for the whole package."""

from __future__ import annotations

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core.text import looks_like_artifact as _looks_like_artifact
from stone_pipeline.matching import projections as proj
from stone_pipeline.stages.curate.attributes import build_attribute_curation, write_attribute_curation
from stone_pipeline.stages.curate.context import CurationResult, alias_model as _alias_model
from stone_pipeline.stages.curate.existing import ImportFile, load_all_existing, load_existing
from stone_pipeline.stages.curate.keys import BRANCHES, active_branches, gen_key, image_filename, image_url
from stone_pipeline.stages.curate.output import write_curation
from stone_pipeline.stages.curate.pipeline import build_curation, run
from stone_pipeline.stages.curate.review import code_reason as _code_reason, review_evidence as _review_evidence

__all__ = [
    "BRANCHES", "CurationResult", "ImportFile", "SETTINGS", "active_branches", "build_attribute_curation",
    "build_curation", "gen_key", "image_filename", "image_url", "load_all_existing", "load_existing", "proj",
    "run", "write_attribute_curation", "write_curation",
    "_alias_model", "_code_reason", "_looks_like_artifact", "_review_evidence",
]
