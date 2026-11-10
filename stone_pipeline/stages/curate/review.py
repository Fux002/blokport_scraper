"""Review cards: the ONE shape every hold takes, the scraped evidence it carries, and the hold outcomes a
gapped row can land on. A hold explains WHY the operator sees the card; it never guesses on their behalf."""

from __future__ import annotations

import re

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import FlagCode
from stone_pipeline.core.text import title_case
from stone_pipeline.matching import projections as proj
from stone_pipeline.stages.product_state import sku_from


def attr_surface(row, vocab: str) -> str:
    """Attribute value for a new variety's backbone/import row: the resolved
    canonical name, else the raw value surfaced for review. A value normalization
    deliberately dropped (synonym_none/empty, e.g. finish 'Other') is NOT resurfaced
    from the raw field -- only genuinely-unrecognized values (e.g. 'Orchid') surface."""
    name = getattr(row, f"{vocab}_name", "") or ""
    if name:
        return name
    if (getattr(row, f"{vocab}_method", "") or "") in ("synonym_none", "empty"):
        return ""
    return getattr(row, f"raw_{vocab}", "") or ""


def review_evidence(row) -> dict:
    """The scraped EVIDENCE surfaced on a review hold, so a human can judge the correct type/variety
    (which of the Gneiss/Granite/Marble/Onyx 'Aqua Blue' this product is) from the actual product, not
    just the name: the source, the ORIGINAL supplier listing url (so the operator can open the product
    and verify it before minting), ONE product image, and the supplier description. All optional -- empty
    when the row carried none (e.g. an API-only source with no public product page leaves src_url empty).

    The image PREFERS the raw supplier url over the processed improved/ S3 key. The improved/ render is
    coupled to the Medusa product lifecycle -- a Blokport clear/remove hard-deletes the product AND its
    improved/ image, leaving the review queue pointing at a since-deleted key (404) until the next produce
    re-uploads it. The supplier url lives on the supplier CDN, so it survives product churn, it is the
    actual scraped photo (a watermark is fine for internal verification), and it exists for a NOT-yet-minted
    variety (a variant texture does not). Falls back to the improved/ key only when no raw url was captured."""
    imgs = (getattr(row, "raw_image_urls", None) or []) or (getattr(row, "image_keys", None) or [])
    return {"src": getattr(row, "src_site", "") or "",
            # the scraped spelling a decision is keyed on (the matcher's query), distinct from the cleaned
            # identity the card is titled with ('Amazon Green Granite' vs 'Amazon Green')
            "scraped": getattr(row, "variety_match_key", "") or getattr(row, "raw_name", "") or "",
            "src_url": getattr(row, "src_url", "") or "",
            "image": next((u for u in imgs if u), ""),
            "description": _evidence_description(getattr(row, "description", "") or "")}


def _evidence_description(text: str, limit: int = 400) -> str:
    """The operator-facing description snippet on a review card: markup-STRIPPED and length-CAPPED. A source
    whose description is page-builder markup (e.g. Divi '[et_pb_...]' shortcodes, ~17 KB each) would otherwise
    bloat the review-queue payload -- the mint page loads a card per held variety, so ~1000 such cards is a
    ~18 MB response the page times out on and shows nothing. Strip shortcodes + HTML, collapse whitespace,
    cap the length; the operator only needs a readable snippet to judge the variety, not the raw page."""
    t = re.sub(r"\[/?[^\]]*\]", " ", text or "")   # WordPress/Divi shortcodes: [et_pb_...] and [/...]
    t = re.sub(r"<[^>]+>", " ", t)                  # HTML tags
    return re.sub(r"\s+", " ", t).strip()[:limit]


def review_card(kind: str, variant: str, reason: str, evidence: dict, *, stone_type: str = "", color: str = "",
                nearest_existing: str = "", score="", model_prob="") -> dict:
    """One shape for every review-queue card (variants_to_confirm) so the operator queue is uniform no
    matter which hold path built it, and no arm can silently omit a column. `kind` names WHY the card exists
    (new, similar, collision, no_type, new_type, code, retired; the origin queue adds origin; the VE2 freeze
    adds archived_type, archived_value and archived_variety -- see ARCHIVED_AND_INTERNAL.md; a guessed colour
    adds colour_unconfirmed):
    it explains, it never limits what the operator may state. `evidence` is review_evidence()
    (src/scraped/src_url/image/description); its keys never collide with the card fields, so the spread is
    additive."""
    return {"confirm": "", "kind": kind, "variant": variant, "reason": reason, "stone_type": stone_type,
            "color": color, "nearest_existing": nearest_existing, "score": score,
            "model_prob": model_prob, **evidence}


def colour_cards(rows, ref) -> list[dict]:
    """The colour_unconfirmed holds (reconcile: no vendor colour, a variety sold in several): ONE card per (vendor,
    variety, type), the one question an operator answers once for all of that vendor's listings of the variety. It
    carries what the answer needs: every listing it covers (`listings`, each scraped spelling, which the decision is
    stated on), their SKUs and count, the colours to pick from (the variety's backbone colours, then the pack's
    multicolour value) and the stone's photo. `ref_scope` keeps one vendor's card apart from another's of the same
    variety (stages.decisions.write_confirm_file). No colour is prefilled: the card never guesses."""
    groups: dict[tuple[str, str, str], list] = {}
    for row in rows:
        if any(f.code == FlagCode.colour_unconfirmed for f in row.review_flags):
            key = (proj.norm(row.src_site), proj.norm(row.variation_name), proj.norm(row.type_name))
            groups.setdefault(key, []).append(row)
    codes: dict[str, str] = {}
    cards = []
    for key in sorted(groups):
        members = groups[key]
        src = members[0].src_site or ""
        code = codes.setdefault(src, load_source(src).source_code)
        members.sort(key=lambda r: sku_from(code, r.surrogate_key))
        first = members[0]
        variant, stone_type = first.variation_name or "", first.type_name or ""
        variety = ref.backbone.lookup(variant, stone_type=stone_type)
        allowed = list(dict.fromkeys(variety.colors if variety else []))
        multi = active_pack().multicolour_value
        colours = allowed + ([multi] if multi and proj.norm(multi) not in {proj.norm(c) for c in allowed} else [])
        listings = sorted({(src, review_evidence(r)["scraped"]) for r in members if review_evidence(r)["scraped"]})
        skus = list(dict.fromkeys(sku_from(code, r.surrogate_key) for r in members))
        evidence = {**review_evidence(first), "image": _stone_photo(members)}
        named = next((f.raw_value for r in members for f in r.review_flags
                      if f.code == FlagCode.multicolour_from_vendor), None)
        why = (f"Colour unconfirmed: {src} names several colours ('{named}') and there is no multicolour value"
               if named else f"No vendor colour: {src} gives none and {variant} comes in {human_join(allowed)}")
        cards.append({**review_card(
            "colour_unconfirmed", variant,
            f"{why}, so none is guessed. Pick this supplier's {variant} colour; it applies to all {len(skus)} "
            f"listing(s). A listing already in the catalog keeps its colour until then; a new one waits.",
            evidence, stone_type=stone_type),
            "ref_scope": src, "listings": [{"source": s, "scraped": sp} for s, sp in listings],
            "colours": colours, "skus": skus, "count": len(skus)})
    return cards


def _stone_photo(members) -> str:
    """The stone's photo for a colour card: its standardised main view when one is published (images.standardised
    puts it first in image_keys), else the scraped lead photo; the first listing (by SKU) that has one."""
    for row in members:
        if row.standard_photo and row.image_keys:
            return row.image_keys[0]
    return next((u for row in members for u in (row.raw_image_urls or []) if u), "")


def human_join(items: list[str]) -> str:
    """'A' / 'A and B' / 'A, B, and C' -- a readable list for a review reason string."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def code_reason(code_why: str, base: str) -> str:
    """The review 'reason' for a code-shaped name: says what it looks like AND what to do, naming the
    likely base variety so the operator can alias in one read."""
    kind = ("a trailing grade letter (e.g. 'Rosal C')" if code_why == "lone_letter"
            else "a supplier code")
    lead = f"Looks like {kind}, not a variety name."
    if base:
        return f"{lead} Alias it to '{title_case(base)}' if it is that stone, otherwise reject."
    return f"{lead} Alias it to the real variety if it is a spelling of one, otherwise reject."


def named_with_types(c, name: str) -> str:
    """'Name (Type1, Type2)' for the review's nearest-existing column, so the operator sees which type(s)
    a name exists under. Bare name when it exists under none / is unknown here."""
    if not name:
        return ""
    types = sorted({o[1] for o in c.existing_surface.get(proj.norm(name), set())})
    return (f"{title_case(name)} ({human_join([title_case(t) for t in types])})" if types
            else title_case(name))


# --- the HOLD outcomes one gapped row can take --------------------------------------------------------------
def hold_archived_variety(c, clean: str, variety_name: str, variety_type: str, row) -> None:
    """VE2 PR5: a gapped scrape matches an ARCHIVED variety. The alias row is still emitted (reactivating the
    variety auto-binds the listing and this card clears on the next produce), but a NEW alias onto a frozen
    variety never reaches the export the matcher reads -- the lane never serves an archived variation -- so
    the listing would otherwise stay a tree gap forever with no card. Surface it once, here. The card carries
    the owner's TYPE so it has its own (name, type) ref: write_confirm_file never merges it into a type-less
    collision card of the same name, and a decide gets the type prefilled."""
    c.pending_confirm.append(review_card("archived_variety", title_case(clean),
        f"This listing matches {variety_name}, which is archived in Medusa. Bind it to an active variety or "
        f"mint it as new in Review, or reactivate {variety_name} in Blokport.",
        review_evidence(row), stone_type=title_case(variety_type)))


def hold_collision(c, clean: str, stone_type: str, owner_names: list[str], row) -> None:
    """ONE surface, SEVERAL varieties: the operator picks which variety it is (globally, or for this vendor
    only), or mints it new. The one review card for every collision, whichever path found it."""
    fam = human_join([title_case(n) for n in owner_names])
    c.pending_confirm.append(review_card("collision", clean,
        f"Matches several existing varieties ({fam}). Alias it to the right one, or mint as new.",
        review_evidence(row), stone_type=stone_type, nearest_existing=fam))


def known_as(clean: str, owners: list[tuple[str, str]]) -> tuple[str, str]:
    """How a scraped name is already known, from its (norm name, norm type) owners: ("'X' already exists as
    Granite and Marble", "X (Granite, Marble)") when varieties of that NAME exist; ("'X' is an alias of
    'Absolut' (Onyx)", "Absolut (Onyx)") when the name is only an ALIAS on other varieties; both joined when
    both apply. The operator must be told which variety a card is really pointing at: a name that exists
    nowhere as a variety must never be described as one."""
    n = proj.norm(clean)
    same = sorted({t for name, t in owners if name == n})
    alias_of = sorted({(name, t) for name, t in owners if name != n})
    phrases, nearest = [], []
    if same:
        phrases.append(f"'{title_case(clean)}' already exists as {human_join([title_case(t) for t in same])}")
        nearest.append(f"{title_case(clean)} ({human_join([title_case(t) for t in same])})")
    if alias_of:
        named = human_join([f"'{title_case(name)}' ({title_case(t)})" for name, t in alias_of])
        phrases.append(f"'{title_case(clean)}' is an alias of {named}")
        nearest += [f"{title_case(name)} ({title_case(t)})" for name, t in alias_of]
    return " and ".join(phrases), "; ".join(nearest)


def hold_for_type(c, clean: str, row, owners: list[tuple[str, str]]) -> None:
    """A type-less scrape whose name is a REAL variety (or alias) under SEVERAL stone types: hold it for the
    human to assign the correct type, surfacing the candidates -- never a typeless clone onto an arbitrary
    type."""
    phrase, nearest = known_as(clean, owners)
    c.pending_confirm.append(review_card("no_type", clean,
        f"{phrase}. Pick one of those types to add this to the existing variety, or choose a different "
        f"type to create a new one.",
        review_evidence(row), color=title_case(attr_surface(row, "color")), nearest_existing=nearest))


def hold_new_type(c, clean: str, stone_type: str, row, owners: list[tuple[str, str]]) -> None:
    """HOLD-never-guess: the scrape carries a stone type NOT among the types this name is already known
    under ('Ocean Blue' exists as granite/marble, scrape says quartzite; or the name is only an alias on an
    Onyx). A mis-tag would become a phantom, so the operator confirms a genuinely new variety (mint) or
    rejects it. A mint decision on this name un-holds it."""
    phrase, nearest = known_as(clean, owners)
    c.pending_confirm.append(review_card("new_type", clean,
        f"{phrase}, but this scrape is typed '{title_case(stone_type)}'. Confirm it is a genuinely NEW "
        f"variety to mint '{title_case(clean)} {title_case(stone_type)}', or reject it as a mis-tag.",
        review_evidence(row), stone_type=title_case(stone_type),
        color=title_case(attr_surface(row, "color")), nearest_existing=nearest))


def hold_retired(c, title: str, stone_type: str, obs_color: str, evidence: dict) -> None:
    """Keep-retired-and-surface: a confirmed mint whose deterministic Key lands on a RETIRED variety.
    Retirement is sticky, so neither silently skip it (its rows would gap every run with no signal) nor let
    it slip into the update delta -- surface an explicit un-retire decision instead."""
    c.pending_confirm.append(review_card("retired", title,
        f"'{title}' was previously RETIRED. Un-retire it to bring it back; it will not be "
        f"re-created otherwise.",
        evidence, stone_type=title_case(stone_type), color=title_case(obs_color or ""),
        nearest_existing=named_with_types(c, title)))


def hold_code(c, clean: str, code_why: str, base: str, stone_type: str, row) -> None:
    # an UNDECIDED code-shaped name -> hold for review (an operator mint/reject was already honoured)
    c.pending_confirm.append(review_card("code", clean, code_reason(code_why, base), review_evidence(row),
                                         stone_type=stone_type, nearest_existing=named_with_types(c, base)))


def hold_similar(c, clean: str, stone_type: str, nearest: str, row, gap, prob: float) -> None:
    near_types = sorted({o[1] for o in c.existing_surface.get(proj.norm(nearest), set())})
    type_note = f" ({human_join([title_case(t) for t in near_types])})" if near_types else ""
    pick_type = " and pick the matching type" if len(near_types) > 1 else ""
    c.pending_confirm.append(review_card("similar", clean,
        f"Very similar to existing '{title_case(nearest)}'{type_note}. If it is "
        f"the same stone, alias it to '{title_case(nearest)}'{pick_type}. Mint "
        f"as a new variety only if it is genuinely different.",
        review_evidence(row), stone_type=stone_type, color=gap.suggested_color or "",
        nearest_existing=named_with_types(c, nearest), score=gap.nearest_score or "",
        model_prob=round(prob, 2)))


def hold_new(c, clean: str, stone_type: str, nearest: str, row, gap) -> None:
    # Per the 'every new variety is reviewed' policy a new variety is a review decision, never a silent
    # auto-mint (an operator 'yes' already minted it, a 'no' already rejected it).
    c.pending_confirm.append(review_card("new", clean,
        "New variety (no close existing match). Confirm to add it as a new variety, or reject.",
        review_evidence(row), stone_type=stone_type, color=title_case(attr_surface(row, "color")),
        nearest_existing=named_with_types(c, nearest) if nearest else "", score=gap.nearest_score or ""))
