"""Minting: the last-resort creation of a genuinely NEW variety (phase 4) -- its ONE variety row, its backbone
post and an image item per category its products sit in -- plus the backbone leaf suggestions (phase 5) and the
result counts. Every mint edge (type-less, already-exists, retired) is enforced here, at ONE point."""

from __future__ import annotations

from stone_pipeline.config.categories import active_categories
from stone_pipeline.config.domain import active_pack
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.core.text import UnfoldableName, clean_alias_list, title_case
from stone_pipeline.matching import projections as proj
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference.loaders import field_kinds, restricted_values
from stone_pipeline.stages.curate.context import Curation, CurationResult, spelling_of, variety_identity
from stone_pipeline.stages.curate.review import (attr_surface, hold_retired, human_join, named_with_types,
                                                 review_card, review_evidence)
from stone_pipeline.aliases.learned import is_learned

log = logfmt.get_logger("curate")


def mint(c: Curation, clean: str, stone_type: str, row, gap) -> None:
    """Create a NEW variety row (clean + stone_type) -- the last-resort mint, shared with the operator-confirm
    path so a confirmed NEW type on an existing multi-type name mints DIRECTLY.

    MINT + RENAME: a mint decision may carry a corrected NAME (seed_name, keyed by the scraped spelling). The
    variety is then minted under that display name -- Name AND Key -- and the scraped spelling is recorded as
    its alias, so the product binds on the next produce through the alias surface exactly like every alias
    does. Two different spellings renamed to ONE name mint one variety: the second only adds its spelling."""
    name = spelling_of(row)
    renamed = c.decisions.seed_name(row.src_site, name, clean) or ""
    try:
        # the stated name whenever there is one: a spelling with no ASCII form keys like its stated ASCII name
        display = field_kinds.display(field_kinds.VARIATION_NAME, renamed or clean)
    except UnfoldableName:
        # Never minted under a half-folded name: the operator states the variety's ASCII name on a `new` card, and
        # the next produce mints under it (the existing card kind, so the review UI needs nothing new).
        c.pending_confirm.append(review_card(
            "new", clean, "New variety with no ASCII form for its name: state the variety's name to add it.",
            review_evidence(row), stone_type=stone_type, color=title_case(attr_surface(row, "color"))))
        return
    owner = (proj.norm(display), proj.norm(stone_type))
    if proj.norm(display) != proj.norm(clean) and not c.decisions.scope(row.src_site, name, clean):
        # the scraped spelling rides onto the mint row as its own alias (owner[0] == norm(title)); for a
        # variety that does not exist yet emit_alias_rows finds no existing row and skips it, as intended.
        # A rename made FOR one vendor attaches nothing here: that vendor's scoped alias binds its products.
        c.alias_new.setdefault(owner, {}).setdefault(clean, (row.src_site, None))   # the mint row carries it (W3)
    if owner in c.minted_display:
        return                          # a second spelling of the same renamed variety: alias only
    c.minted_display.add(owner)
    c.new_variant_rows.append((
        clean, display, stone_type,
        title_case(attr_surface(row, "color")),
        (row.quality_name or c.last_resort_quality).strip() or c.last_resort_quality,
        title_case(attr_surface(row, "finish")), gap,
        c.variety_categories.get(proj.norm(clean), set()),
        review_evidence(row), name, row.src_site or "",
    ))


# --- phase 4: emit genuinely-new varieties (variety row + backbone post + image items) ------------------
def _observed_attributes(c: Curation) -> dict[tuple[str, str], dict[str, set]]:
    # Observed attributes per new variety: the UNION across EVERY product that maps to it, not just the one
    # row that triggered the mint. Otherwise a variety is born with one product's colour/finish/quality and
    # leaf-gaps every sibling that differs, or gets an empty colour set when its trigger product had none.
    # SAME identity as the dedup/mint (variety_identity), so it keys to exactly the variety that gets minted.
    obs_union: dict[tuple[str, str], dict[str, set]] = {}
    for r in c.rows:
        nm, st, clean = variety_identity(c, r)
        if not nm:
            continue
        u = obs_union.setdefault((proj.norm(st), proj.norm(clean)),
                                 {"colors": set(), "qualities": set(), "finishes": set()})
        if r.color_name:
            u["colors"].add(r.color_name.strip())
        if r.quality_name:
            u["qualities"].add(r.quality_name.strip())
        if r.finish_name:
            u["finishes"].add(r.finish_name.strip())
    return obs_union


def _displayed(field: str, values) -> list[str]:
    """`values` written by `field`'s rule, in order, de-duplicated; a value with no ASCII form is dropped and logged,
    so the caller's fallback (the pack's colour, the last-resort quality) still applies."""
    out: list[str] = []
    for value in values:
        try:
            out.append(field_kinds.display(field, value))
        except UnfoldableName:
            log.warning("mint value dropped: no ASCII form", extra={"extra_fields": {"field": field, "value": value}})
    return list(dict.fromkeys(out))


def _alias_display(text: str) -> str:
    return field_kinds.display(field_kinds.VARIATION_ALIAS_VALUE, text)


def emit_new_variants(c: Curation, result: CurationResult) -> None:
    # A variety is the same material in any format: ONE record per (type, name), whatever category its
    # products sit in. Its Key is the one rule (new_key), so a variety the existing index already carries under
    # that Key is never minted twice.
    existing_keys = {v["Key"] for v in c.existing.varieties}
    active = {cat.name for cat in active_categories()}
    obs_union = _observed_attributes(c)
    pack = active_pack()
    # VE2 PR5: a restricted (archived/internal) stone type cannot be minted (it drives the Key, a frozen
    # identity), and restricted colours/qualities are dropped from a mint. From the files via restricted_values
    # (the single authority); empty in the expand phase, so minting is byte-identical until the columns exist.
    restricted = restricted_values()
    for name, title, stone_type, obs_color, obs_quality, obs_finish, gap, observed, evidence, spelling, src in c.new_variant_rows:
        if not stone_type:
            # a variety cannot mint without a stone type (it drives the Key, so a wrong/empty type is a wrong
            # identity). HOLD it for the operator to assign one via the review (seed_type) instead of guessing
            # or shipping it type-less. The single enforcement point for the invariant.
            c.pending_confirm.append(review_card("no_type", title,
                "No stone type detected. Assign the correct type to mint it. "
                "A variety cannot exist without a type.",
                evidence, color=title_case(obs_color or ""),
                nearest_existing=named_with_types(c, gap.nearest_existing) if gap else ""))
            continue
        # VE2 PR5: the restriction holds below apply ONLY to a variety that would ACTUALLY mint (it is new). An
        # EXISTING variety re-enters emit_new_variants every produce (a stored mint decision); holding or
        # re-typing it would flood the queue and, by following the card, mint a DUPLICATE under a wrong type.
        # So decide first whether it would mint.
        key = new_key(stone_type, title)
        would_mint = key not in existing_keys
        # a restricted stone type holds through the SAME one review path (pending_confirm). The Key is the
        # variety's frozen identity, so a NEW variety must not be minted under an archived/internal type.
        if would_mint and proj.norm(stone_type) in restricted.get("type", set()):
            c.pending_confirm.append(review_card("archived_type", title,
                "This variety's stone type is archived or internal in Medusa, so it cannot be minted under it. "
                "Assign an active type in Review, or reactivate the type in Blokport.",
                evidence, stone_type=stone_type, color=title_case(obs_color or ""),
                nearest_existing=named_with_types(c, gap.nearest_existing) if gap else ""))
            continue
        # keep-retired-and-surface: a mint whose deterministic Key is RETIRED was deliberately retired; surface
        # ONE un-retire decision and skip the variety (a retired stone is never resurrected).
        if key in c.retired_keys:
            hold_retired(c, title, stone_type, obs_color, evidence)
            continue
        if not would_mint:
            continue  # the variety exists: nothing to emit
        # observed attributes and the seed colour are looked up by the SCRAPED identity (`name`, the cleaned
        # spelling), not the display title: for a renamed mint a title-keyed lookup would drop them.
        u = obs_union.get((proj.norm(stone_type), proj.norm(name)),
                          {"colors": set(), "qualities": set(), "finishes": set()})
        # colour is REQUIRED for a Medusa product; a source like zucchi often supplies none. An operator-chosen
        # mint colour WINS over the observed/fallback chain (by the scraped spelling, then the cleaned identity;
        # never a colour another vendor's statement chose), else the pack's fallback colour so the variety and
        # its products are always priceable.
        seeded = c.decisions.seed_color(src, spelling, name)
        colors = ([seeded] if seeded
                  else _displayed(field_kinds.COLOR_NAME, sorted(u["colors"]))
                  or _displayed(field_kinds.COLOR_NAME, [obs_color] if obs_color else []) or [pack.fallback_color])
        qualities = (_displayed(field_kinds.QUALITY_NAME, sorted(u["qualities"]))
                     or _displayed(field_kinds.QUALITY_NAME, [obs_quality]) or [c.last_resort_quality])
        finishes = list(dict.fromkeys([*_displayed(field_kinds.FINISH_NAME,
                                                   [*sorted(u["finishes"]), *([obs_finish] if obs_finish else [])]),
                                       *pack.default_finishes]))
        # VE2 PR5: drop restricted colours/qualities from the mint (the classify palette's restricted colours
        # land here too, via the observed set). Dropping must NEVER fall back to a pack default -- that would
        # GUESS a value into output and override an operator statement (binding invariant). If a drop empties a
        # required dimension, HOLD on the same review path, naming the restricted value, for the operator to
        # pick an active one; a non-empty remainder mints as-is. Dormant: nothing restricted -> nothing dropped.
        rc, rq = restricted.get("color", set()), restricted.get("quality", set())
        dropped_colors = [x for x in colors if x and proj.norm(x) in rc]
        dropped_quals = [x for x in qualities if x and proj.norm(x) in rq]
        colors = [x for x in colors if not (x and proj.norm(x) in rc)]
        qualities = [x for x in qualities if not (x and proj.norm(x) in rq)]
        if (dropped_colors and not colors) or (dropped_quals and not qualities):
            parts: list[str] = []
            if dropped_colors and not colors:
                verb = "are" if len(dropped_colors) > 1 else "is"
                label = "colours" if len(dropped_colors) > 1 else "colour"
                parts.append(f"The only {label} this variety would mint with "
                             f"({human_join([title_case(v) for v in dropped_colors])}) {verb} archived or "
                             "internal in Medusa. Pick an active colour in Review, or reactivate it in Blokport.")
            if dropped_quals and not qualities:
                verb = "are" if len(dropped_quals) > 1 else "is"
                label = "qualities" if len(dropped_quals) > 1 else "quality"
                parts.append(f"The only {label} this variety would mint with "
                             f"({human_join([str(v) for v in dropped_quals])}) {verb} archived or internal in "
                             "Medusa. Reactivate it in Blokport, or the supplier's listing changes.")
            # the colour field is left EMPTY on the card: prefilling the restricted colour would just hold again.
            c.pending_confirm.append(review_card("archived_value", title, " ".join(parts), evidence,
                stone_type=stone_type,
                nearest_existing=named_with_types(c, gap.nearest_existing) if gap else ""))
            continue
        # The spellings recorded for THIS new variety's own (name, type) owner (alias_new): a renamed mint's
        # scraped spelling rides on its row, so its product resolves in ONE upload. Match the FULL owner: a
        # same-name variety of another type must not inherit it.
        own_aliases, dropped = clean_alias_list(title, sorted({s for owner, sp in c.alias_new.items()
                                                       if owner == (proj.norm(title), proj.norm(stone_type))
                                                       for s in sp}), _alias_display)
        if dropped:
            log.warning("mint aliases dropped: no ASCII form", extra={"extra_fields": {"variant": title,
                                                                                       "dropped": dropped}})
        type_name = c.type_names[proj.norm(stone_type)]       # gated canonical above: the Medusa type NAME
        # the categories its scraped products sit in (spec section 4, N3): the icons made for it, one per pair
        categories = sorted(observed & active)
        result.new_variants.append({
            "Key": key,
            "Name": title,
            "Type": type_name,
            "Aliases": "|".join(own_aliases),
            # gap diagnostics are absent for an operator-override mint (a matched row has no gap)
            "_nearest_existing": (gap.nearest_existing or "") if gap else "",
            "_nearest_score": (gap.nearest_score if gap.nearest_score is not None else "") if gap else "",
            "_suggested_type": stone_type,
            "_example_url": (gap.example_src_url or "") if gap else "",
        })
        result.backbone_new.append({
            "key": key,  # the clean, unique join between backbone and Medusa export
            "variant": title,
            "stone_type": type_name,
            "color": colors,
            "finishes": finishes,
            "qualities": qualities,
            "aliases": list(own_aliases),
            "exists": "no",
            "in_csv": "yes",
            "categories": categories,
            "merged_from": [],
        })
        result.images_to_generate += [{"key": key, "category": cat, "variant": title, "status": "to_generate"}
                                      for cat in categories]


# --- phase 5: backbone leaf suggestions (an existing variety sold in a not-yet-allowed value) -------------
def leaf_updates(rows: list[CanonicalRow], result: CurationResult) -> None:
    seen_leaf: set[tuple] = set()
    for row in rows:
        for g in row.tree_gaps:
            if g.gap_kind != GapKind.missing_leaf_child:
                continue
            for attribute, value in (("color", g.suggested_color), ("finish", g.suggested_finish),
                                     ("quality", g.suggested_quality)):
                if not value:
                    continue
                # (variety, type) is the identity: a same-name variety of another type is its own card
                key = (proj.norm(row.variation_name or ""), proj.norm(row.type_name or ""), attribute,
                       proj.norm(value))
                if key in seen_leaf:
                    continue
                seen_leaf.add(key)
                result.backbone_updates.append({
                    "variety": row.variation_name or "",
                    "stone_type": row.type_name or "",   # disambiguates same-named varieties
                    "attribute": attribute,
                    "add_value": value,
                    "currently_allowed": g.nearest_existing or "",
                    "match_method": row.variation_method or "",
                    "match_confidence": row.variation_confidence or "",
                    # the same rule as the alias write-back: a guessed bind (is_learned) is verified first
                    "verdict": "verify_match" if is_learned(row.variation_method) else "likely_real",
                    "example_url": g.example_src_url or "",
                })


def finish(c: Curation, result: CurationResult) -> None:
    result.counts = {
        "learned_proposals": len(result.learned_proposals),
        "new_variants": len(result.new_variants),
        "distinct_new_varieties": len(c.new_variant_rows),
        "backbone_updates": len(result.backbone_updates),
        "images_to_generate": len(result.images_to_generate),
        "suspicious_names": len(c.suspicious),
        "pending_confirm": len(c.pending_confirm),
    }
    # A review card whose own triggering row carried no photo falls back to a sibling row's image (same
    # variety). Purely cosmetic post-fill -- never overwrites an image the row had.
    for p in c.pending_confirm:
        if not p.get("image"):
            p["image"] = c.variety_images.get(proj.norm(p.get("variant", "")), "")
    result.suspicious_names = c.suspicious   # written to review by write_curation
    result.pending_confirm = c.pending_confirm
    # Defence in depth: the mint-emission HOLD makes a type-less variety impossible here; surface it LOUD if
    # a future change ever regresses, rather than shipping a type-less (bad-Key) variety to Medusa.
    typeless = [p["variant"] for p in result.backbone_new if not p.get("stone_type")]
    if typeless:
        log.error("type-less varieties reached the mint; should have been HELD for seed_type",
                  extra={"extra_fields": {"count": len(typeless), "sample": typeless[:5]}})
