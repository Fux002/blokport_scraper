"""Minting: the last-resort creation of a genuinely NEW variety (phase 4) -- the import row per active
category, its backbone post, and its image entry -- plus the backbone leaf suggestions (phase 5) and the
result counts. Every mint edge (type-less, already-exists, retired) is enforced here, at ONE point."""

from __future__ import annotations

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import category
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.core.text import title_case
from stone_pipeline.matching import projections as proj
from stone_pipeline.stages.curate.context import (Curation, CurationResult, decided, spelling_of,
                                                  variety_identity)
from stone_pipeline.stages.curate.keys import BRANCHES, active_branches, core, gen_key, image_filename, image_url
from stone_pipeline.stages.curate.review import (attr_surface, hold_retired, named_with_types, review_card,
                                                 review_evidence)

log = logfmt.get_logger("curate")


def mint(c: Curation, clean: str, stone_type: str, row, gap) -> None:
    """Create a NEW variety row (clean + stone_type) -- the last-resort mint, shared with the operator-confirm
    path so a confirmed NEW type on an existing multi-type name mints DIRECTLY.

    MINT + RENAME: a mint decision may carry a corrected NAME (seed_name, keyed by the scraped spelling). The
    variety is then minted under that display name -- Name AND Key -- and the scraped spelling is recorded as
    its alias, so the product binds on the next produce through the alias surface exactly like every alias
    does. Two different spellings renamed to ONE name mint one variety: the second only adds its spelling."""
    name = spelling_of(row)
    renamed = decided(c.seed_names, row.src_site, name, clean) or ""
    display = title_case(renamed) if renamed and proj.norm(renamed) != proj.norm(clean) else title_case(clean)
    owner = (proj.norm(display), proj.norm(stone_type))
    if proj.norm(display) != proj.norm(clean) and not decided(c.seed_scopes, row.src_site, name, clean):
        # the scraped spelling rides onto the mint row via sib_aliases (owner[0] == norm(title)); for a
        # variety that does not exist yet emit_alias_rows finds no import row and skips it, as intended.
        # A rename made FOR one vendor attaches nothing here: that vendor's scoped alias binds its products.
        c.alias_new.setdefault(owner, set()).add(title_case(clean))
    if owner in c.minted_display:
        return                          # a second spelling of the same renamed variety: alias only
    c.minted_display.add(owner)
    c.new_variant_rows.append((
        clean, display, stone_type,
        title_case(attr_surface(row, "color")),
        (row.quality_name or c.last_resort_quality).strip() or c.last_resort_quality,
        title_case(attr_surface(row, "finish")), gap,
        c.variety_branches.get(proj.norm(clean), set()),
        review_evidence(row), name, row.src_site or "",
    ))


# --- phase 4: emit genuinely-new variants (import row + backbone post + image entry) -----------------------
def _union_cores(c: Curation) -> set[str]:
    # A variety belongs to the STONE, not a format: if it exists in ANY active category it exists in all,
    # because the emit's union-fill materializes the row for every category it is missing from. So the
    # "already exists" set is the UNION of cores (type+name slug, branch-independent) across every category:
    # curate never re-mints a variety any category already carries, and a sparse live export (a category
    # Medusa has not fully populated) still counts its varieties via the categories that do carry them.
    cores: set[str] = set()
    for b in BRANCHES:
        cores |= {core(v["Key"], b) for v in c.imports[b].varieties if v.get("Key")}
    return cores


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
            u["colors"].add(title_case(r.color_name))
        if r.quality_name:
            u["qualities"].add(r.quality_name.strip())
        if r.finish_name:
            u["finishes"].add(title_case(r.finish_name))
    return obs_union


def emit_new_variants(c: Curation, result: CurationResult) -> None:
    # A variety is the same material in any format, so it is emitted into every ACTIVE category's import
    # file (the backbone is identical across categories).
    union_cores = _union_cores(c)
    obs_union = _observed_attributes(c)
    pack = active_pack()
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
        # keep-retired-and-surface: a mint whose deterministic Key in ANY fan-out branch is RETIRED was
        # deliberately retired; surface ONE un-retire decision and skip the whole variety (retirement is
        # per-variety, so a retired stone is never resurrected in another format).
        if any(gen_key(b, stone_type, title) in c.retired_keys for b in active_branches()):
            hold_retired(c, title, stone_type, obs_color, evidence)
            continue
        # observed attributes and the seed colour are looked up by the SCRAPED identity (`name`, the cleaned
        # spelling), not the display title: for a renamed mint a title-keyed lookup would drop them.
        u = obs_union.get((proj.norm(stone_type), proj.norm(name)),
                          {"colors": set(), "qualities": set(), "finishes": set()})
        # colour is REQUIRED for a Medusa product; a source like zucchi often supplies none. An operator-chosen
        # mint colour WINS over the observed/fallback chain (by the scraped spelling, then the cleaned identity;
        # never a colour another vendor's statement chose), else the pack's fallback colour so the variety and
        # its products are always priceable.
        seeded = decided(c.seed_colors, src, spelling, name)
        colors = ([seeded] if seeded
                  else sorted(u["colors"]) or ([obs_color] if obs_color else []) or [pack.fallback_color])
        qualities = sorted(u["qualities"]) or [obs_quality]
        finishes = list(dict.fromkeys([*sorted(u["finishes"]),
                                       *([obs_finish] if obs_finish else []), *pack.default_finishes]))
        # A cross-branch sibling's variety already exists in another branch carrying the scraped spelling as
        # an alias (alias_new): carry that SAME alias onto the new sibling so its product resolves in ONE
        # upload. Match the FULL (name, type) owner: a same-name variety of another type must not inherit it.
        sib_aliases = sorted({s for owner, sp in c.alias_new.items()
                              if owner == (proj.norm(title), proj.norm(stone_type)) for s in sp})
        for branch in active_branches():
            key = gen_key(branch, stone_type, title)
            if core(key, branch) in union_cores:
                continue  # variety already exists in this branch; do not duplicate it
            fname = image_filename(key)
            product_backed = branch in observed  # a scraped product uses this branch
            result.new_variants[branch].append({
                "Key": key,
                "Name": title,
                # only link an image when a product uses this branch; fan-out copies stay imageless until a
                # product adds them (lazy generation)
                "Image": image_url(fname) if product_backed else "",
                "Aliases": "|".join(sib_aliases),
                "Volume per kg (m³/kg)": category(branch).volume_per_kg,
                # gap diagnostics are absent for an operator-override mint (a matched row has no gap)
                "_nearest_existing": (gap.nearest_existing or "") if gap else "",
                "_nearest_score": (gap.nearest_score if gap.nearest_score is not None else "") if gap else "",
                "_suggested_type": stone_type,
                "_example_url": (gap.example_src_url or "") if gap else "",
            })
            result.backbone_new[branch].append({
                "key": key,  # the clean, unique join between backbone and Medusa export
                "variant": title,
                "category": category(branch).label,
                "stone_type": stone_type,
                "color": colors,
                "finishes": finishes,
                "qualities": qualities,
                "aliases": list(sib_aliases),
                "image_file": fname,  # same name as the variant Image, for consistency
                "exists": "no",
                "in_csv": "yes",
                "product_backed": product_backed,  # only generate images for product-backed
                "merged_from": [],
            })
            if product_backed:      # no image generation for a fan-out copy with no product
                result.images_to_generate.append({
                    "image_filename": fname,
                    "s3_url": image_url(fname),
                    "variant": title,
                    "category": category(branch).label,
                    "status": "to_generate",
                })


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
                key = (proj.norm(row.variation_name or ""), attribute, proj.norm(value))
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
                    "verdict": "likely_real" if (row.variation_method or "").startswith(("exact", "projection"))
                    else "verify_match",
                    "example_url": g.example_src_url or "",
                })


def finish(c: Curation, result: CurationResult) -> None:
    result.counts = {
        "alias_additions": sum(len(v) for v in result.alias_additions.values()),
        "confirmed_alias_additions": sum(1 for v in result.alias_additions.values()
                                         for r in v if r.get("_status") == "confirmed"),
        "new_variants": sum(len(v) for v in result.new_variants.values()),
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
    typeless = [p["variant"] for posts in result.backbone_new.values() for p in posts if not p.get("stone_type")]
    if typeless:
        log.error("type-less varieties reached the mint; should have been HELD for seed_type",
                  extra={"extra_fields": {"count": len(typeless), "sample": typeless[:5]}})
