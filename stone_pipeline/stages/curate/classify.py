"""Phase 2: classify each gapped variety, in STRICT PRIORITY ORDER.

The rule for the ordering: do the cheapest, most DECISIVE thing first, and REUSE before MINT.
  CANONICALISE the name -> DEDUP one decision per identity -> REJECT/HOLD junk (never mint) -> RESOLVE to an
  EXISTING variety (exact surface, then the fuzzy nearest) -> MINT as the last resort (held for review).
Uncertain at any resolve/mint step -> HOLD for human review, never guess."""

from __future__ import annotations

import re

from stone_pipeline.core.schema import GapKind
from stone_pipeline.core.text import looks_code_shaped, looks_like_artifact
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import type_slug_from_key
from stone_pipeline.stages.curate.aliasing import collision_owners
from stone_pipeline.stages.curate.context import (Curation, is_generic, is_token_subset, level,
                                                  variety_identity, surface_owners)
from stone_pipeline.stages.curate.minting import mint
from stone_pipeline.stages.curate.review import (hold_archived_variety, hold_code, hold_collision,
                                                 hold_for_type, hold_new, hold_new_type, hold_similar,
                                                 review_evidence)
from stone_pipeline.stages.format_resolve import branch_of


def observe_categories_and_images(c: Curation) -> None:
    # which categories a gapped variety was ACTUALLY observed in (has a scraped product): a new variety is ONE
    # record, and these are the categories its icons are made for. Keyed by the CLEANED variety name (the
    # minted identity), so it lines up with the new-variant dedup even when two raw names clean to the same
    # variety.
    for row in c.rows:
        if not any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps):
            continue
        _, _, clean = variety_identity(c, row)
        if clean:
            c.variety_categories.setdefault(proj.norm(clean), set()).add(branch_of(row))
    # First non-empty evidence image PER variety (across ALL its rows, gapped or not): a review card shows
    # the triggering row's photo, but that row may have none while a sibling row of the same variety has
    # one. Cosmetic only: the image is operator-verification evidence, never data.
    for row in c.rows:
        _, _, clean = variety_identity(c, row)
        k = proj.norm(clean)
        if clean and not c.variety_images.get(k) and (img := review_evidence(row).get("image")):
            c.variety_images[k] = img


def classify(c: Curation) -> None:
    for row in c.rows:
        gaps = [g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation]
        gap = gaps[0] if gaps else None
        # CANONICALISE: 'Azul White Quartzite' (typed Quartzite) cleans to 'Azul White' and mints as
        # quartzite, not under the wrong 'Onyx' tag.
        name, stone_type, clean = variety_identity(c, row)
        if not name:
            continue
        # An explicit operator statement OVERRIDES the matcher's auto-bind: a row the matcher resolved (no
        # gap) still enters classification when the operator decided its scraped spelling is a mint FOR
        # THIS vendor. The mint arm `continue`s before any gap dereference; a matched row with no own
        # decision keeps its match.
        if gap is None and c.decisions.confirm(row.src_site, name, clean) != "yes":
            continue
        # DEDUP: one decision per cleaned identity, keyed on (RESOLVED type, cleaned name, decision level)
        # -- the SAME identity gen_key uses, so two same-named varieties of DIFFERENT types are EACH minted,
        # and a vendor's own stone is a separate identity from the global meaning (see level). A name with no
        # usable key (a non-Latin script only, empty folded key) has no identity to share: deduping on "" would
        # pool every such name of one type into the first and silently drop the rest, so each one goes on.
        if key := proj.norm(clean):
            identity = (proj.norm(stone_type), key, level(c, row.src_site, name, clean))
            if identity in c.seen_new:
                continue
            c.seen_new.add(identity)
        if _reject_or_decide(c, row, gap, name, stone_type, clean):
            continue
        if _resolve_existing(c, row, gap, name, stone_type, clean):
            continue
        if _resolve_nearest(c, row, gap, name, stone_type, clean):
            continue
        # a genuinely NEW, UNDECIDED variety: held for the operator. A TYPE-LESS one is not gated here: it
        # falls through to mint -> the type-less hold at the single enforcement point.
        if stone_type:
            hold_new(c, clean, stone_type, (gap.nearest_existing or "").split(",")[0].strip(), row, gap)
            continue
        mint(c, clean, stone_type, row, gap)


def _reject_or_decide(c: Curation, row, gap, name: str, stone_type: str, clean: str) -> bool:
    """REJECT / HOLD gates (never mint), cheapest + most decisive FIRST, before any reuse-or-mint decision so
    junk can't be matched, aliased, or minted; then the operator's own MINT decision, honoured HERE and
    ONLY here for every row, before any similarity arm can alias or re-review it away. True = handled."""
    if looks_like_artifact(clean):            # not a variety name at all (code artifact)
        c.suspicious.append({"src_site": row.src_site, "raw_name": name, "cleaned_name": clean})
        return True
    if c.decisions.is_rejected(row.src_site, name, clean):    # a human said 'no' on a past run
        return True
    if c.decisions.confirm(row.src_site, name, clean) == "yes":
        # every mint edge (type-less, already-exists, retired) is handled at the single enforcement point
        # in emit_new_variants, so mint is safe to call directly
        mint(c, clean, stone_type, row, gap)
        return True
    # code-SHAPED names ('Rosal C', 'Trani Bianco H', 'Gs') are supplier codes/grades, not varieties ->
    # NEVER mint them. A trailing lone-letter grade whose de-coded base is a KNOWN, single, NON-colour
    # variety is auto-aliased to that variety ('Rosal C' -> 'Rosal'); the colour guard means a colour-named
    # variety is never merged ('White G' is too bare to auto-resolve -> review). Everything else -> review.
    code_why = looks_code_shaped(clean)
    base = re.sub(r"[\s\-]+[A-Za-z]\s*$", "", clean).strip() if code_why == "lone_letter" else ""
    if code_why == "lone_letter":
        bnorm = proj.norm(base)
        btoks = set(bnorm.split())
        owners = c.existing_surface.get(bnorm)
        if owners and len(owners) == 1 and btoks and not (btoks <= c.generic_words):
            c.alias_new.setdefault(next(iter(owners)), {}).setdefault(name, (row.src_site, None))   # the real variety
            return True
    if code_why:
        hold_code(c, clean, code_why, base, stone_type, row)
        return True
    return False


def _archived_owner_name(c: Curation, owner: tuple[str, str]) -> str | None:
    """The display name of an alias OWNER (norm name, norm type) that is archived in Medusa, else None. VE2:
    a NEW alias onto an archived variety never serves, so classify surfaces a card instead of a silent gap.
    A variety is one record whatever its category, so its archived flag is the one record's."""
    e = c.existing.by_name_type.get(owner)
    return e["Name"] if (e and e.get("archived")) else None


def _resolve_existing(c: Curation, row, gap, name: str, stone_type: str, clean: str) -> bool:
    """RESOLVE to an EXISTING variety by exact surface (an alias beats a new variant). A generic colour+type
    trade name is its OWN variety and skips alias routing (never promoted into a premium named stone).
    Owners are (name, TYPE), so a multi-type name is routed BY TYPE, never collapsed onto an arbitrary
    same-name variety of a different stone:
      * product's type IS an existing type       -> alias to THAT variety (identity beats alias; several
                                                    same-type owners = a trade-name COLLISION, held)
      * type-less                                 -> ALWAYS review to set the type (one variety: pick a type;
                                                    an alias family: which variety)
      * product carries a NEW type                -> HOLD (a mis-tag would become a phantom)
    The matcher's own COLLISION verdict names the owners on the gap, so a collision never falls to the fuzzy
    aliaser. True = handled."""
    if is_generic(c, clean):
        return False
    # the matcher's COLLISION verdict names the owners on the gap; they WIDEN the cleaned surface's own owners,
    # never lose to them. Consulting them only when the cleaned surface had none let a narrower surface (the
    # type-stripped spelling owned by ONE of two same-type varieties) turn an explicit collision into an alias
    # that already existed: no card, no mint, the row held on every produce with nothing to decide.
    owners = surface_owners(c, clean) | collision_owners(c, row, gap)
    if not owners:
        return False
    st = proj.norm(stone_type)
    same_type = sorted(o for o in owners if st and o[1] == st)
    if same_type:
        named = next((o for o in same_type if o[0] == proj.norm(clean)), None)
        if named or len({o[0] for o in same_type}) == 1:
            owner = named or same_type[0]
            c.alias_new.setdefault(owner, {}).setdefault(name, (row.src_site, None))
            if (vname := _archived_owner_name(c, owner)):   # VE2: alias still recorded; surface the hold
                hold_archived_variety(c, clean, vname, owner[1], row)
        else:
            hold_collision(c, clean, stone_type, sorted({o[0] for o in same_type}), row)
        return True
    if not st:
        # A type the scrape did not make clear is NEVER auto-completed, not even to a single existing
        # same-name pair: (type, variant) is the unique identity; the operator sets the type via review.
        if len({o[0] for o in owners}) == 1:
            hold_for_type(c, clean, row, sorted(owners))
        else:
            hold_collision(c, clean, "", sorted({o[0] for o in owners}), row)
        return True
    hold_new_type(c, clean, stone_type, row, sorted(owners))
    return True


def _canonical_types(c: Curation, nn: str) -> set[str]:
    """The types of the existing varieties NAMED nn (not those merely carrying it as an alias)."""
    return {t for n, t in c.existing_surface.get(nn, set()) if n == nn}


def _resolve_nearest(c: Curation, row, gap, name: str, stone_type: str, clean: str) -> bool:
    """RESOLVE against the fuzzy nearest existing variety: the tier-7 model uses name + type + colour to tell
    a real alias ('Marjan' -> 'Marjan Silver') from a distinct sibling ('Cristallo Divine' vs 'Cristallo
    Bianco'): P>=hi auto-confirms the alias, P<=lo mints, the uncertain middle goes to review. Falls back to
    the flat fuzzy floor + token subset when the model isn't available. True = handled."""
    nearest = (gap.nearest_existing or "").split(",")[0].strip()
    if not nearest or is_generic(c, clean):
        return False
    if c.resolver is not None:
        # The nearest is ONE variety, (name, type): its type is the matcher candidate's Key type (the type
        # authority), never looked up by name, since several types share a name ('Aqua Blue' granite, marble).
        nn = proj.norm(nearest)
        nt = proj.norm(type_slug_from_key(gap.nearest_key)) if gap.nearest_key else ""
        nc, nal = c.near_meta.get((nn, nt), ([], []))
        d = c.resolver.decide_against(clean, stone_type, [gap.suggested_color or ""], [nearest, *nal], nt, nc)
        # A confident alias is confirmed onto that variety only when its (name, type) is certain: the
        # candidate's Key named the type, and the row's own type agrees with it (decide_against gated a
        # cross-type alias to review) or, for a TYPE-LESS row, the name is held by that one type only.
        # Otherwise the confirmation would pick the row's type from whichever same-name variety the fuzzy
        # tier happened to rank first, so it is held for the operator to state the variety instead.
        if d.verdict == "alias" and nt and (stone_type or len(_canonical_types(c, nn)) == 1):
            # the judge's probability, on the score scale (0..100) every learned alias is scored on
            c.alias_new.setdefault((nn, nt), {}).setdefault(name, (row.src_site, round(d.prob * 100, 2)))
            if (vname := _archived_owner_name(c, (nn, nt))):   # VE2: fuzzy-confirmed alias onto archived
                hold_archived_variety(c, clean, vname, nt, row)
            return True
        if d.verdict in ("alias", "review"):
            hold_similar(c, clean, stone_type, nearest, row, gap, d.prob)
            return True
        return False                                # verdict "mint" -> a new variety
    if (gap.nearest_score or 0) >= c.alias_floor or is_token_subset(clean, nearest):
        c.review_candidates.setdefault(proj.norm(nearest), {}).setdefault(name, (row.src_site, gap.nearest_score))
        return True
    return False
