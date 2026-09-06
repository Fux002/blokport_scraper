"""Stage 4: resolve variation (section 7 Stage 4, engine in section 5A).

Category scoped: the branch (a registry category name) selects that category's
variant index (ref.variants[name], from the ONE combined export), because the same
variety has a different variation id per category. One engine per category, built
from the registry. Standalone categories (not stone varieties) route to their own
matcher via STANDALONE_MATCHERS instead. The matcher blocks on category, then type
and colour; the input is variety_match_key; aliases participate in every tier.

Outcomes (section 5A.3):
  - at or above auto_accept: accept, set method to the winning tier, write the
    scraped spelling back as a new alias (in-memory for this run AND persisted to
    state/ for the next) so it is an exact alias hit next time.
  - in the review band: route to review with the top candidates.
  - below the floor or no candidate: route to the tree-gap queue as
    missing_variation, carrying the nearest candidate and score.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from stone_pipeline.config.settings import SETTINGS, Confidence, bulk_form_name, category
from stone_pipeline.core import logfmt
from stone_pipeline.core.manifest import StageMetric
from stone_pipeline.adapters.tokens import clean_variety
from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind, ReviewFlag, TreeGap
from stone_pipeline.gates.report import DEGRADED, OK
from stone_pipeline.stages._rowguard import isolate_rows
from stone_pipeline.matching.engine import VariationEngine, VariationMatch
from stone_pipeline.matching.index import build_variation_index
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import ReferenceData
from stone_pipeline.state.writeback import WriteBack, load_alias_writeback

log = logfmt.get_logger("match_variation")

# Plug-in matchers for STANDALONE categories (shares_variety_vocab=False, e.g.
# accessories), keyed by category name. A matcher is `fn(row, ref) -> None` and
# sets row.variation_id / variation_name / variation_method (or adds a gap). With
# none registered, a standalone row is held as unsupported_category. This is the
# seam where an accessory vertical attaches its own name/SKU matching -- no edit
# to this stage required. See CATEGORY_GUIDE.md.
STANDALONE_MATCHERS: dict = {}   # category name -> fn(row, ref)


def _apply_writeback(index, entries: list[tuple[str, str]]) -> None:
    for vid, alias in entries:
        index.add_surface_alias(vid, alias)


def _build_suggester(index):
    """Build the tier-8 semantic suggester only when enabled (section 5A.2)."""
    if not SETTINGS.matching.enable_semantic:
        return None
    from stone_pipeline.matching.semantic import SemanticSuggester, sentence_transformer_embedder

    names, cids = [], []
    for cid, cand in index.candidates.items():
        for surface in cand.surfaces:
            names.append(surface)
            cids.append(cid)
    embedder = sentence_transformer_embedder(SETTINGS.matching.semantic_model)
    return SemanticSuggester(names, cids, embedder)


@dataclass
class VariationStage:
    """Holds the per-branch engines and indexes for a run, so alias write-back
    accumulates across rows (section 5A.3, 13A.1: build the index once)."""

    ref: ReferenceData
    engines: dict[str, VariationEngine]   # category name -> engine (one per registry vocab category)
    writeback: WriteBack
    generic_descriptor: bool = False

    @classmethod
    def build(cls, ref: ReferenceData, writeback: WriteBack | None = None,
              writeback_path=None, generic_descriptor: bool = False) -> "VariationStage":
        thresholds = SETTINGS.thresholds
        # Stick to the category: each branch matches only against its OWN variants
        # (no cross-category borrowing). One engine per registry category; a new
        # category gets its engine automatically from ref.variants.
        persisted = load_alias_writeback(writeback_path)  # confirmed spellings -> exact hits
        auto, floor = thresholds.variation_auto_accept, thresholds.variation_review_floor
        engines: dict[str, VariationEngine] = {}
        for name, table in ref.variants.items():
            index = build_variation_index(table, ref.backbone)
            _apply_writeback(index, persisted)
            engines[name] = VariationEngine(index, auto, floor, suggester=_build_suggester(index))
        return cls(
            ref=ref, engines=engines,
            writeback=writeback or WriteBack(),
            generic_descriptor=generic_descriptor,
        )

    def _engine(self, branch: str) -> VariationEngine:
        # branches reaching here always have their own engine (each registry vocab
        # category is loaded); the fallback is a defensive default, registry-agnostic.
        return self.engines.get(branch) or next(iter(self.engines.values()))

    def _key_for(self, cid: str | None) -> str | None:
        """The matched variety's STABLE Key for a Medusa variation id. The product links to its
        variety by this Key, NOT by the id: variation.medusa_id churns (export id -> minted id on
        ack), so an id-based link orphans a later scrape's products. The Key never changes."""
        if not cid:
            return None
        for table in self.ref.variants.values():
            v = table.by_id.get(cid)
            if v is not None:
                return v.key
        return None

    def resolve_row(self, row: CanonicalRow) -> None:
        # branch comes from the Format Resolver (run before this stage). Fall back
        # to the raw tag only when the format stage has not run (unit tests).
        from stone_pipeline.stages.format_resolve import branch_of

        if not row.format_value and row.raw_format:
            row.format_value = row.raw_format.strip().title()
        branch = branch_of(row)
        row.is_block = branch == bulk_form_name()
        cat = category(branch)

        # A standalone category (own vocabulary, not stone varieties -- e.g.
        # accessories) does not use the stone-variety engine. Its matcher plugs in
        # via STANDALONE_MATCHERS; with none registered the row is held (not
        # mismatched to a stone variety) until that category's vertical is built.
        if cat and not cat.shares_variety_vocab:
            matcher = STANDALONE_MATCHERS.get(cat.name)
            if matcher is not None:
                matcher(row, self.ref)
                return
            row.variation_method = "unsupported_category"
            row.add_gap(TreeGap(
                src_site=row.src_site, surrogate_key=row.surrogate_key or "",
                raw_name=row.raw_name or "",
                normalized_name=proj.norm(row.variety_match_key or row.raw_name or ""),
                suggested_type=row.raw_type, suggested_color=row.color_name or row.raw_color,
                gap_kind=GapKind.unsupported_category,
                nearest_existing=f"({branch} matching not built yet)",
                example_src_url=row.src_url,
            ))
            return

        engine = self._engine(branch)

        # A category that is not yet ACTIVATED (no Medusa pcat) and has no reference
        # is held in the gap queue rather than borrowing another category's id. Once
        # activated, it flows through normal matching against its (initially empty)
        # reference, so every row gaps as missing_variation and mints a variant (each
        # category's engine is isolated, so no other category's id is ever borrowed).
        if cat and not cat.active and not engine.index.candidates:
            row.variation_method = "category_not_activated"
            row.add_gap(TreeGap(
                src_site=row.src_site, surrogate_key=row.surrogate_key or "",
                raw_name=row.raw_name or "",
                normalized_name=proj.norm(row.variety_match_key or row.raw_name or ""),
                suggested_type=row.raw_type, suggested_color=row.color_name or row.raw_color,
                gap_kind=GapKind.missing_tile_reference,
                nearest_existing="(tile variation export not supplied)",
                example_src_url=row.src_url,
            ))
            return

        # override is the top strategy: a human-set variation_id always wins
        overrides = self.ref.overrides
        if overrides is not None:
            forced = overrides.get(row.src_site, row.surrogate_key or "", "variation_id")
            if forced:
                cand = engine.index.candidates.get(forced)
                key = self._key_for(forced)
                if cand is not None and key:
                    row.variation_id = forced
                    row.variation_key = key
                    row.variation_name = cand.canonical
                    row.variation_confidence = Confidence.high.name
                    row.variation_method = "override"
                    return
                # The override id is NOT a candidate in THIS branch (a stale pre-remint id, or a slab id on
                # the block branch). Committing it anyway stamped variation_id=forced with a NULL key at high
                # confidence -- an orphan link that bypasses the gap/review path and FK-fails on ack. Ignore
                # the unusable override and fall through to the normal match/gap path, loudly.
                log.warning("variation_id override ignored: id is not a candidate in this branch",
                            extra={"extra_fields": {"forced": forced, "src_site": row.src_site,
                                                    "surrogate_key": row.surrogate_key}})

        query = (row.variety_match_key or "").strip()
        if not query:
            # The clean extraction yielded no variety. For a generic-descriptor
            # source, try the colour+type name (minus format) as a strict,
            # canonical-name-only match: a real variety whose name happens to be a
            # colour+type pair (White Travertine, Pink Onyx) resolves, while a
            # truly generic descriptor (Cream Marble) or a generic backend alias
            # (White Marble -> White Namibe) does not, and gaps instead.
            if self.generic_descriptor:
                if self._try_canonical_name(row, engine):
                    return
            self._gap(row, VariationMatch(None, None, Confidence.none, "no_variety", 0.0, []))
            return

        block_color = row.color_name or row.raw_color or ""
        # block by the CORRECTED canonical type (type_name), not the raw scrape tag -- candidate
        # block_type is the canonical variety.stone_type, so the raw tag mis-blocks (a mis-tagged
        # 'Azul White Quartzite' under an Onyx tag, or a name-corrected type).
        scraped_type = row.type_name or row.raw_type or ""
        match = engine.match(query, block_type=scraped_type, block_color=block_color)
        # OPERATOR-TYPE FALLBACK: when the scrape's type finds NO home (a genuine no-candidate gap, never a
        # same-type ambiguity), the operator's MINT decision for this variety is the authority -- re-match
        # under the operator's type so the product binds to the operator-minted (name, type) instead of
        # gapping forever (its scraped type matches neither the minted variety nor any other). This is the
        # one place a mint decision reaches a PRODUCT, mirroring how curate mints the variety; from here the
        # bound row flows through the SAME reconcile/derive/texture path a suggested variant uses. A scraped
        # type that DID match a variety is never overridden. Keyed by the shared clean-variety identity, so
        # the lookup key matches the one curate/decisions_store store the decision under.
        # The `not (scraped_type and match.ambiguous)` gate enforces the "never a same-type ambiguity" rule
        # above: a genuine tied same-name duplicate (2+ varieties of the scraped type sharing one canonical,
        # detected at the exact OR the fuzzy tier and flagged match.ambiguous) must SURFACE for review, not be
        # silently bound/escaped by a retry under a different (cleaned or operator) identity. Only a real
        # no-candidate miss retries.
        if match.cid is None and not (scraped_type and match.ambiguous):
            clean = clean_variety(query, scraped_type)
            # CLEAN-NAME RETRY: the scrape's match key carries the supplier's type token ('Crystal White
            # Granite'). That full string exists only as an ALIAS on sibling same-type varieties (Bianco,
            # Storen), never on the CANONICAL variety of that exact name (which lists no '<name> <type>'
            # alias) -- so the exact tier sees several different-canonical candidates and gaps, and the
            # variety re-queues for mint every produce even though it already exists. Retry with the type-
            # STRIPPED name under the SAME scraped type: the engine's identity-beats-alias narrowing then
            # keeps the canonical owner and binds it ('Crystal White' -> the canonical Crystal White granite).
            # Identity stays (type, name): the scraped type still blocks, so a genuinely new-type scrape
            # ('Imperial Blue' quartzite, existing only as granite) still gaps and holds for review.
            if proj.norm(clean) != proj.norm(query):
                retry = engine.match(clean, block_type=scraped_type, block_color=block_color)
                if retry.cid is not None:
                    match = VariationMatch(retry.cid, retry.canonical, retry.confidence,
                                           f"clean_variety_{retry.method}", retry.score, retry.candidates)
            # OPERATOR-TYPE FALLBACK: still no home under the scraped type -- the operator's MINT decision is
            # the authority. Retry under the operator's type AND the cleaned identity name: the cleaned name
            # 'Absolute Black' + the operator type binds at the exact tier -- exactly the (name, type) the
            # operator minted. A scraped type that DID match a variety is never overridden.
            if match.cid is None:
                op_type = self.ref.variety_seed_types.get(proj.norm(clean))
                if op_type and proj.norm(op_type) != proj.norm(scraped_type):
                    retry = engine.match(clean, block_type=op_type, block_color=block_color)
                    if retry.cid is not None:
                        match = VariationMatch(retry.cid, retry.canonical, retry.confidence,
                                               f"operator_type_{retry.method}", retry.score, retry.candidates)

        if match.cid is not None and match.confidence >= Confidence.medium:
            row.variation_id = match.cid
            row.variation_key = self._key_for(match.cid)
            row.variation_name = match.canonical
            row.variation_confidence = Confidence(match.confidence).name
            row.variation_method = match.method
            # write-back: a non-exact confirmed match learns the scraped spelling, PERSISTED for the
            # next run (section 8.4). NOT added to the live index mid-run: doing so made a later
            # identical query resolve via 'exact'(high) instead of 'fuzzy'(medium) purely by batch
            # order -- same cid, but an order-dependent method/confidence that skews review routing.
            # Same-run repeats now resolve identically; the persisted alias takes effect next run.
            if match.method not in ("exact", "override"):
                # record the learning method as provenance -> a persisted fuzzy/phonetic guess is auditable
                # and reversible (state.writeback.forget_alias), never an invisible permanent exact alias.
                self.writeback.add_alias(match.cid, query, method=match.method)
            return

        if match.method in ("review", "semantic_review"):
            row.variation_confidence = "low"
            row.variation_method = match.method
            row.add_flag(
                ReviewFlag(
                    field="variation",
                    code=FlagCode.variation_review,
                    raw_value=query,
                    best_guess=match.candidates[0][1] if match.candidates else None,
                    confidence=Confidence.low,
                    method=f"top:{_fmt_candidates(match.candidates)}",
                    src_url=row.src_url,
                )
            )
            return

        # below floor or no candidate -> gap
        self._gap(row, match)

    def _try_canonical_name(self, row: CanonicalRow, engine: VariationEngine) -> bool:
        """Strict recovery for generic descriptors: match the colour+type name
        (minus the format word) but accept ONLY a deterministic hit on the
        variety's canonical name, never a generic alias or a fuzzy hit. Returns
        True if a variation was accepted."""
        from stone_pipeline.adapters.tokens import strip_format

        candidate_name = strip_format(row.raw_name or "")
        if not candidate_name:
            return False
        block_color = row.color_name or row.raw_color or ""
        match = engine.match(candidate_name, block_type=row.type_name or row.raw_type or "", block_color=block_color)
        # Name identity is the real safety check, not the match tier: when the matched
        # variant's name IS the colour+type candidate, it is that variety even if the
        # engine reached it via phonetic/fuzzy (e.g. "Silver Travertine"). A generic
        # alias to a different name (White Marble -> White Namibe) fails name_match and
        # gaps; a deliberately blocked/ambiguous hit stays below medium and gaps too.
        name_match = match.canonical is not None and proj.norm(match.canonical) == proj.norm(candidate_name)
        if match.cid is not None and name_match and match.confidence >= Confidence.medium:
            row.variation_id = match.cid
            row.variation_key = self._key_for(match.cid)
            row.variation_name = match.canonical
            row.variation_confidence = Confidence(match.confidence).name
            row.variation_method = f"descriptor_{match.method}"
            return True
        # a near-but-not-canonical hit is a review candidate, not an accept
        if match.cid is not None or match.candidates:
            row.variation_confidence = "low"
            row.variation_method = "review_generic"
            best = match.canonical or (match.candidates[0][1] if match.candidates else None)
            row.add_flag(ReviewFlag(field="variation", code=FlagCode.variation_review,
                                    raw_value=candidate_name, best_guess=best,
                                    confidence=Confidence.low, method=f"{match.method}",
                                    src_url=row.src_url))
            return True  # routed to review (held), do not also gap
        return False

    def _gap(self, row: CanonicalRow, match: VariationMatch) -> None:
        row.variation_method = match.method
        nearest = match.candidates[0] if match.candidates else None
        row.add_gap(
            TreeGap(
                src_site=row.src_site,
                surrogate_key=row.surrogate_key or "",
                raw_name=row.raw_name or "",
                normalized_name=proj.norm(row.variety_match_key or row.raw_name or ""),
                suggested_type=row.raw_type,
                suggested_color=row.color_name or row.raw_color,
                suggested_finish=row.finish_name or row.raw_finish,
                suggested_quality=row.quality_name or row.raw_quality,
                gap_kind=GapKind.missing_variation,
                nearest_existing=nearest[1] if nearest else None,
                nearest_score=round(nearest[2], 1) if nearest else None,
                example_src_url=row.src_url,
            )
        )


def _fmt_candidates(candidates: list[tuple[str, str, float]]) -> str:
    return ", ".join(f"{name}({score:.0f})" for _, name, score in candidates[:3])


def run(rows: list[CanonicalRow], ref: ReferenceData, writeback: WriteBack | None = None,
        writeback_path=None, generic_descriptor: bool = False) -> StageMetric:
    stage = VariationStage.build(ref, writeback=writeback, writeback_path=writeback_path,
                                 generic_descriptor=generic_descriptor)
    isolated = isolate_rows(rows, "match_variation", stage.resolve_row, log)
    # The Resolve layer is the essential-complexity core: surface its own health. A high UNMATCHED share
    # on an established source signals a tree/index/vocab drift (not just new varieties), caught here.
    # review rows are held (not gapped); gapped rows carry a tree gap for the operator worklist.
    resolved = sum(1 for r in rows if r.variation_id)
    review = sum(1 for r in rows if any(f.code == FlagCode.variation_review for f in r.review_flags))
    gapped = sum(1 for r in rows if not r.variation_id and r.tree_gaps)
    unmatched = len(rows) - resolved
    methods = dict(Counter(r.variation_method for r in rows if r.variation_method))
    thr = SETTINGS.thresholds
    status = DEGRADED if rows and (unmatched / len(rows) >= thr.match_unmatched_degraded
                                   or isolated / len(rows) >= thr.row_isolated_degraded) else OK
    log.info(
        "variation done",
        extra={"extra_fields": {"rows": len(rows), "resolved": resolved, "gapped": gapped,
                                "isolated": isolated}},
    )
    return StageMetric(stage="match_variation", status=status, rows_in=len(rows), rows_out=len(rows),
                       reviewed=review, gapped=gapped,
                       extra={"resolved": resolved, "methods": methods, "isolated": isolated})
