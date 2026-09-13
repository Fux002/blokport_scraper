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
from stone_pipeline.stages.curate.aliasing import alias_and_backfill, collision_owners
from stone_pipeline.stages.curate.context import (Curation, decided, is_generic, is_token_subset, level,
                                                  variety_identity)
from stone_pipeline.stages.curate.minting import mint
from stone_pipeline.stages.curate.review import (hold_code, hold_collision, hold_for_type, hold_new,
                                                 hold_new_type, hold_similar, review_evidence)
from stone_pipeline.stages.format_resolve import branch_of


def observe_branches_and_images(c: Curation) -> None:
    # which branches a gapped variety was ACTUALLY observed in (has a scraped product). A variety is still
    # created in every active category for a uniform catalog, but only its product-backed branch needs an
    # image generated. Keyed by the CLEANED variety name (the minted identity), so it lines up with the
    # new-variant dedup even when two raw names clean to the same variety.
    for row in c.rows:
        if not any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps):
            continue
        _, _, clean = variety_identity(c, row)
        if clean:
            c.variety_branches.setdefault(proj.norm(clean), set()).add(branch_of(row))
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
        if gap is None and decided(c.confirm_decisions, row.src_site, name, clean) != "yes":
            continue
        # DEDUP: one decision per cleaned identity, keyed on (RESOLVED type, cleaned name, decision level)
        # -- the SAME identity gen_key uses, so two same-named varieties of DIFFERENT types are EACH minted,
        # and a vendor's own stone is a separate identity from the global meaning (see level).
        identity = (proj.norm(stone_type), proj.norm(clean), level(c, row.src_site, name, clean))
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
    if decided(c.rejected, row.src_site, name, clean):        # a human said 'no' on a past run
        return True
    if decided(c.confirm_decisions, row.src_site, name, clean) == "yes":
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
            c.alias_new.setdefault(next(iter(owners)), set()).add(name)   # auto-alias to the real variety
            return True
    if code_why:
        hold_code(c, clean, code_why, base, stone_type, row)
        return True
    return False


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
    owners = c.existing_surface.get(proj.norm(clean)) or collision_owners(c, row, gap)
    if not owners:
        return False
    st = proj.norm(stone_type)
    same_type = sorted(o for o in owners if st and o[1] == st)
    if same_type:
        named = next((o for o in same_type if o[0] == proj.norm(clean)), None)
        if named or len({o[0] for o in same_type}) == 1:
            alias_and_backfill(c, named or same_type[0], name, clean, stone_type, row, gap)
        else:
            hold_collision(c, clean, stone_type, sorted({o[0] for o in same_type}), row)
        return True
    if not st:
        # A type the scrape did not make clear is NEVER auto-completed, not even to a single existing
        # same-name pair: (type, variant) is the unique identity; the operator sets the type via review.
        if len({o[0] for o in owners}) == 1:
            hold_for_type(c, clean, row, sorted({o[1] for o in owners}))
        else:
            hold_collision(c, clean, "", sorted({o[0] for o in owners}), row)
        return True
    hold_new_type(c, clean, stone_type, row, sorted({o[1] for o in owners}))
    return True


def _resolve_nearest(c: Curation, row, gap, name: str, stone_type: str, clean: str) -> bool:
    """RESOLVE against the fuzzy nearest existing variety: the tier-7 model uses name + type + colour to tell
    a real alias ('Marjan' -> 'Marjan Silver') from a distinct sibling ('Cristallo Divine' vs 'Cristallo
    Bianco'): P>=hi auto-confirms the alias, P<=lo mints, the uncertain middle goes to review. Falls back to
    the flat fuzzy floor + token subset when the model isn't available. True = handled."""
    nearest = (gap.nearest_existing or "").split(",")[0].strip()
    if not nearest or is_generic(c, clean):
        return False
    if c.resolver is not None:
        nt, nc, nal = c.near_meta.get(proj.norm(nearest), ("", [], []))
        d = c.resolver.decide_against(clean, stone_type, [gap.suggested_color or ""], [nearest, *nal], nt, nc)
        if d.verdict == "alias":
            # confident -> confirm; attach to the nearest variety of its OWN type, so a spelling lands on
            # the right stone, not an arbitrary same-name one.
            c.alias_new.setdefault((proj.norm(nearest), proj.norm(nt)), set()).add(name)
            return True
        if d.verdict == "review":
            hold_similar(c, clean, stone_type, nearest, row, gap, d.prob)
            return True
        return False                                # verdict "mint" -> a new variety
    if (gap.nearest_score or 0) >= c.alias_floor or is_token_subset(clean, nearest):
        c.review_candidates.setdefault(proj.norm(nearest), set()).add(name)
        return True
    return False
