"""Alias routing: which scraped spellings attach to which EXISTING variety, by its (name, type) owner --
from the rows the matcher already resolved (phase 1) and from the classification's resolve arms -- and the
emission of the alias additions in the variety-file shape, one row per variety (phase 3)."""

from __future__ import annotations

from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import type_slug_from_key
from stone_pipeline.stages.curate.context import Curation, CurationResult, spelling_of
from stone_pipeline.stages.curate.existing import alias_list
from stone_pipeline.state.writeback import is_learned


def by_name_owner(c: Curation, nm: str, prefer_type: str = "") -> tuple[str, str] | None:
    """The (name, TYPE) owner for a bare variety NAME (a matched row missing its variation_key): the row's
    own resolved type disambiguates a multi-type name; else exactly ONE existing variety carrying nm as its
    canonical name; else None -- genuinely ambiguous, the caller HOLDS, never an arbitrary stone. Owners are
    filtered to canonical-name matches so a name that is another variety's ALIAS does not falsely make its
    own canonical owner look ambiguous."""
    same_name = {o for o in c.existing_surface.get(nm, set()) if o[0] == nm}
    pt = proj.norm(prefer_type)
    if pt and (typed := next((o for o in same_name if o[1] == pt), None)):
        return typed
    return next(iter(same_name)) if len(same_name) == 1 else None


# --- phase 1: alias additions from the rows the MATCHER already resolved (or held for review) -------------
def collect_matched_aliases(c: Curation) -> None:
    for row in c.rows:
        spelling = spelling_of(row)
        if not spelling:
            continue
        # ONE rule for "this bind was a guess worth keeping as an alias", shared with the write-back
        # (is_learned: a phonetic or fuzzy tier, through the clean_variety_/operator_type_ wrappers). Every
        # other bind (exact, projection, override, descriptor) is re-derived from the seed each run, so
        # proposing it as an alias would only make a pick outlive a later seed correction.
        if row.variation_id and is_learned(row.variation_method):
            # attach to the variety the row MATCHED, by its (name, Key-type) -- the Key is the authority;
            # fall back to the by-name owner when the match carries no key (defensive / test rows).
            nnm = proj.norm(row.variation_name or "")
            owner = (nnm, proj.norm(type_slug_from_key(row.variation_key))) if row.variation_key \
                else by_name_owner(c, nnm, row.type_name or row.raw_type or "")
            if owner:
                c.alias_new.setdefault(owner, {}).setdefault(spelling, (row.src_site, None))
        elif (row.variation_method or "") in ("review", "semantic_review", "review_generic"):
            for flag in row.review_flags:
                if flag.field == "variation" and flag.best_guess:
                    c.review_candidates.setdefault(proj.norm(flag.best_guess), {}).setdefault(
                        spelling, (row.src_site, None))


def collision_owners(c: Curation, row, gap) -> set[tuple[str, str]]:
    """The (norm name, norm type) owners the MATCHER listed on a collision gap ('Amazon Green Granite' ->
    Amazon Blue, Amazonia, Verde Ubatuba), resolved to their existing types. Empty for any other gap, so
    only the matcher's explicit verdict widens the surface lookup."""
    if not (row.variation_method or "").endswith("_collision") or not gap.nearest_existing:
        return set()
    return {o for n in gap.nearest_existing.split(",")
            for o in c.existing_surface.get(proj.norm(n.strip()), set())}


def drop_confirmed_from_review(c: Curation) -> None:
    # A spelling already CONFIRMED as an alias of its real owner must NOT also be proposed as a needs-review
    # alias onto a different fuzzy-near variety -- or an operator could rubber-stamp a wrong merge.
    confirmed = {proj.norm(s) for spset in c.alias_new.values() for s in spset}
    c.review_candidates = {tgt: sp for tgt, sp in ((tgt, {s: v for s, v in sp.items() if proj.norm(s) not in confirmed})
                                                   for tgt, sp in c.review_candidates.items()) if sp}


# --- phase 3: propose the learned aliases ----------------------------------------------------------------
def emit_alias_rows(c: Curation, result: CurationResult) -> None:
    """Each spelling curate's arms attached to an EXISTING variety becomes a learned-alias PROPOSAL on that variety's
    Key (P2-10 C2): never an import row, so a guess reaches no seed and no matcher until a review makes it active.
    method 'curate_confirmed' for an arm that named the one owner, 'curate_review' for a needs-review suggestion;
    source is the vendor whose listing proposed it and score the arm's own (0..100) where it has one. A spelling the
    variety already carries is not proposed."""
    def propose(target, spellings: dict[str, tuple], method: str) -> None:
        # target is a (name, type) OWNER (a confirmed alias -> the exact variety) or a bare NAME (a
        # needs-review suggestion, which has no chosen type). Proposals go to the one variety it names.
        existing = (c.existing.by_name_type.get(target) if isinstance(target, tuple)
                    else c.existing.by_name.get(target))
        if not existing:
            return  # no existing variety (a new mint carries its own spellings on its row)
        have = ({proj.norm(a) for a in alias_list(existing["Aliases"])} | {proj.norm(existing["Name"])}
                | {proj.norm(code) for code in alias_list(existing.get("Codes", ""))})
        result.learned_proposals.extend(
            {"identity_key": existing["Key"], "value": spelling, "method": method, "source": spellings[spelling][0],
             "score": spellings[spelling][1]}
            for spelling in sorted(spellings) if proj.norm(spelling) not in have)
    for owner, spellings in c.alias_new.items():                 # owner = (name, type)
        propose(owner, spellings, "curate_confirmed")
    for name_norm, spellings in c.review_candidates.items():     # bare name (no chosen type)
        propose(name_norm, spellings, "curate_review")
