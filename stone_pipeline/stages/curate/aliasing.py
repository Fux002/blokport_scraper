"""Alias routing: which scraped spellings attach to which EXISTING variety, by its (name, type) owner --
from the rows the matcher already resolved (phase 1) and from the classification's resolve arms -- and the
emission of the alias additions in the import-file shape (phase 3)."""

from __future__ import annotations

from stone_pipeline.config.settings import category
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.core.text import title_case
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import type_slug_from_key
from stone_pipeline.stages.curate.context import Curation, CurationResult, spelling_of
from stone_pipeline.stages.curate.existing import alias_list
from stone_pipeline.stages.curate.keys import active_branches, image_filename, image_url
from stone_pipeline.stages.curate.review import attr_surface, review_evidence


def non_exact_confirmed(row: CanonicalRow) -> bool:
    m = row.variation_method or ""
    # descriptor_* matches recovered the variety from an empty variety_match_key, so the
    # only spelling available is the full raw_name (with its format word) -- not a clean
    # alias. Treat them like exact (no alias proposal) so we do not emit "X Marble Slab".
    return bool(row.variation_id) and not (
        m in ("exact", "override") or m.startswith("exact")
        or m.startswith("projection") or m.startswith("descriptor")
    )


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
        if non_exact_confirmed(row):
            # attach to the variety the row MATCHED, by its (name, Key-type) -- the Key is the authority;
            # fall back to the by-name owner when the match carries no key (defensive / test rows).
            nnm = proj.norm(row.variation_name or "")
            owner = (nnm, proj.norm(type_slug_from_key(row.variation_key))) if row.variation_key \
                else by_name_owner(c, nnm, row.type_name or row.raw_type or "")
            if owner:
                c.alias_new.setdefault(owner, set()).add(spelling)
        elif (row.variation_method or "") in ("review", "semantic_review", "review_generic"):
            for flag in row.review_flags:
                if flag.field == "variation" and flag.best_guess:
                    c.review_candidates.setdefault(proj.norm(flag.best_guess), set()).add(spelling)


def collision_owners(c: Curation, row, gap) -> set[tuple[str, str]]:
    """The (norm name, norm type) owners the MATCHER listed on a collision gap ('Amazon Green Granite' ->
    Amazon Blue, Amazonia, Verde Ubatuba), resolved to their existing types. Empty for any other gap, so
    only the matcher's explicit verdict widens the surface lookup."""
    if not (row.variation_method or "").endswith("_collision") or not gap.nearest_existing:
        return set()
    return {o for n in gap.nearest_existing.split(",")
            for o in c.existing_surface.get(proj.norm(n.strip()), set())}


def alias_and_backfill(c: Curation, owner: tuple[str, str], spelling: str, clean: str, stype: str,
                       row, gap) -> None:
    """Attach a scraped spelling to an EXISTING variety (by its (name, type) owner). If a product backs the
    variety in a branch where it does NOT yet exist, also mint the missing sibling so the product resolves
    next round (the existing-cores guard skips branches where it already lives)."""
    c.alias_new.setdefault(owner, set()).add(spelling)
    backed = c.variety_branches.get(proj.norm(clean), set())
    if backed:
        # Backfill the missing cross-branch sibling of the RESOLVED OWNER, under its OWN canonical name --
        # never the scraped spelling. Minting under the alias spelling ('Monalisa') gives the sibling a Key
        # core the existing-cores guard cannot match against the owner ('Mona Lisa'), so it duplicates the
        # owner in every branch. Using the owner name makes the guard skip every branch where the owner
        # already lives and fills only a genuinely-missing branch (carrying the scraped spelling as its
        # alias via sib_aliases, which keys on owner[0] == norm(title)).
        owner_name = next((c.imports[b].by_name_type[owner]["Name"] for b in active_branches()
                           if owner in c.imports[b].by_name_type), title_case(owner[0]))
        c.new_variant_rows.append((owner_name, owner_name, stype,
                                   title_case(attr_surface(row, "color")),
                                   (row.quality_name or c.last_resort_quality).strip() or c.last_resort_quality,
                                   title_case(attr_surface(row, "finish")), gap, backed, review_evidence(row),
                                   spelling, row.src_site or ""))


def drop_confirmed_from_review(c: Curation) -> None:
    # A spelling already CONFIRMED as an alias of its real owner must NOT also be proposed as a needs-review
    # alias onto a different fuzzy-near variety -- or an operator could rubber-stamp a wrong merge.
    confirmed = {proj.norm(s) for spset in c.alias_new.values() for s in spset}
    c.review_candidates = {tgt: sp for tgt, sp in ((tgt, {s for s in sp if proj.norm(s) not in confirmed})
                                                   for tgt, sp in c.review_candidates.items()) if sp}


# --- phase 3: emit alias additions -----------------------------------------------------------------------
def emit_alias_rows(c: Curation, result: CurationResult) -> None:
    def emit(target, spellings: set[str], confirmed: bool) -> None:
        # target is a (name, type) OWNER (a confirmed alias -> the exact variety) or a bare NAME (a
        # needs-review suggestion, which has no chosen type). Attach to the matching variety per branch.
        for branch in active_branches():
            existing = (c.imports[branch].by_name_type.get(target) if isinstance(target, tuple)
                        else c.imports[branch].by_name.get(target))
            if not existing:
                continue  # variety not in this category's import file
            current = alias_list(existing["Aliases"])
            have = {proj.norm(a) for a in current}
            additions = [s for s in sorted(spellings) if proj.norm(s) not in have]
            if not additions:
                continue
            # re-link the clean deterministic image when the variant has one; never copy the export's
            # empty/internal value (would send a blank Image and risk a wipe)
            img = image_url(image_filename(existing["Key"])) if (existing.get("Image") or "").strip() else ""
            result.alias_additions[branch].append({
                "Key": existing["Key"], "Name": existing["Name"], "Image": img,
                "Aliases": "|".join(current + additions),
                "Volume per kg (m³/kg)": existing.get("Volume") or category(branch).volume_per_kg,
                "_added": "|".join(additions),
                "_status": "confirmed" if confirmed else "needs_review",
            })
    for owner, spellings in c.alias_new.items():                 # owner = (name, type)
        emit(owner, spellings, confirmed=True)
    for name_norm, spellings in c.review_candidates.items():     # bare name (no chosen type)
        emit(name_norm, spellings, confirmed=False)
