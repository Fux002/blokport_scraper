"""The curation of one produce, phase by phase, and the stage entry `run` that writes it and audits whether
every stored operator decision was honoured."""

from __future__ import annotations

from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import ReferenceData, type_slug_from_key
from stone_pipeline.stages.curate.aliasing import (collect_matched_aliases, drop_confirmed_from_review,
                                                   emit_alias_rows)
from stone_pipeline.stages.curate.attributes import build_attribute_curation
from stone_pipeline.stages.curate.classify import classify, observe_branches_and_images
from stone_pipeline.stages.curate.context import CurationResult, new_curation
from stone_pipeline.stages.curate.existing import load_all_existing
from stone_pipeline.stages.curate.keys import BRANCHES
from stone_pipeline.stages.curate.minting import emit_new_variants, finish, leaf_updates
from stone_pipeline.stages.curate.output import write_curation

log = logfmt.get_logger("curate")


def build_curation(rows: list[CanonicalRow], ref: ReferenceData) -> CurationResult:
    """The curation of one produce: what the operator must decide (pending cards), which scraped spellings
    attach to existing varieties (alias additions), and which genuinely new varieties are minted (import
    rows + backbone posts + images to generate). Phases in order; see each function."""
    c = new_curation(rows, ref)
    collect_matched_aliases(c)
    observe_branches_and_images(c)
    classify(c)
    drop_confirmed_from_review(c)
    result = CurationResult(
        alias_additions={b: [] for b in BRANCHES},
        new_variants={b: [] for b in BRANCHES},
        backbone_new={b: [] for b in BRANCHES},
    )
    emit_alias_rows(c, result)
    emit_new_variants(c, result)
    leaf_updates(rows, result)
    finish(c, result)
    return result


def run(rows: list[CanonicalRow], ref: ReferenceData) -> CurationResult:
    from stone_pipeline.stages import decisions
    result = build_curation(rows, ref)
    write_curation(result, rows)
    attr = build_attribute_curation(rows, ref)
    # ONE attribute decision file: adopt any Medusa ids you filled in last run -> attributes.csv,
    # then re-list every unresolved colour/finish/type/quality value (new ones to create + id, plus
    # synonym suggestions) in attributes_to_add.csv. No separate synonyms file.
    filled = decisions.load_attribute_ids()
    adopted = decisions.adopt_attribute_ids(filled)
    _ATTR_VOCABS = ("color", "finish", "type", "quality")   # NOT 'variation' -> that's variants_to_confirm
    to_add = [{"medusa_id": "", "kind": a["vocab"], "value": a["raw_value"], "count": a["count"],
               "suggested_value": a["suggested_value"], "action": a["recommended_action"]}
              for a in attr if a["vocab"] in _ATTR_VOCABS
              and (a["vocab"], proj.norm(a["raw_value"])) not in filled]
    decisions.write_attributes_to_add(to_add)
    result.counts["attribute_values"] = len(attr)
    result.counts["attributes_to_add"] = len(to_add)
    result.counts["attributes_adopted"] = adopted
    # Did this produce honour every stored decision? The gaps become cards in the one review list, so a
    # decision the pipeline ignores is visible on the FIRST produce after it, never a day later.
    from stone_pipeline.config import decisions_store
    from stone_pipeline.stages import decision_audit
    existing = {owner for imp in load_all_existing().values() for owner in imp.by_name_type}
    created = {(proj.norm(v["Name"]), proj.norm(type_slug_from_key(v["Key"]).replace("_", " ")))
               for lst in result.new_variants.values() for v in lst}
    pending_spellings = {proj.norm(p["scraped"]) for p in result.pending_confirm if p.get("scraped")}
    gaps = decision_audit.audit(rows, existing, created, decisions_store.variety_actions(),
                                decisions_store.scoped_aliases(), decisions_store.origin_decisions(),
                                pending_spellings)
    decisions.write_decision_gaps(gaps)
    result.counts["decision_gaps"] = len(gaps)
    if gaps:
        log.warning("decisions NOT applied by this produce", extra={"extra_fields": {
            "count": len(gaps), "gaps": [f"{g['decision']} {g['source']} {g['scraped'] or g['name']}" for g in gaps[:20]]}})
    return result
