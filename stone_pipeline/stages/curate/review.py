"""Review cards: the ONE shape every hold takes, the scraped evidence it carries, and the hold outcomes a
gapped row can land on. A hold explains WHY the operator sees the card; it never guesses on their behalf."""

from __future__ import annotations

import re

from stone_pipeline.core.text import title_case
from stone_pipeline.matching import projections as proj


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
    (new, similar, collision, no_type, new_type, code, retired; the origin queue adds origin):
    it explains, it never limits what the operator may state. `evidence` is review_evidence()
    (src/scraped/src_url/image/description); its keys never collide with the card fields, so the spread is
    additive."""
    return {"confirm": "", "kind": kind, "variant": variant, "reason": reason, "stone_type": stone_type,
            "color": color, "nearest_existing": nearest_existing, "score": score,
            "model_prob": model_prob, **evidence}


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
def hold_collision(c, clean: str, stone_type: str, owner_names: list[str], row) -> None:
    """ONE surface, SEVERAL varieties: the operator picks which variety it is (globally, or for this vendor
    only), or mints it new. The one review card for every collision, whichever path found it."""
    fam = human_join([title_case(n) for n in owner_names])
    c.pending_confirm.append(review_card("collision", clean,
        f"Matches several existing varieties ({fam}). Alias it to the right one, or mint as new.",
        review_evidence(row), stone_type=stone_type, nearest_existing=fam))


def hold_for_type(c, clean: str, row, cand_types: list[str]) -> None:
    """A type-less scrape whose name is a REAL variety under SEVERAL stone types: hold it for the human to
    assign the correct type, surfacing the candidate types -- never a typeless clone onto an arbitrary type."""
    types_txt = human_join([title_case(t) for t in cand_types])
    c.pending_confirm.append(review_card("no_type", clean,
        f"'{title_case(clean)}' already exists as {types_txt}. Pick one of those types to add "
        f"this to the existing variety, or choose a different type to create a new one.",
        review_evidence(row), color=title_case(attr_surface(row, "color")),
        nearest_existing=named_with_types(c, clean)))


def hold_new_type(c, clean: str, stone_type: str, row, existing_types: list[str]) -> None:
    """HOLD-never-guess: the scrape carries a stone type NOT among the types this name already exists under
    ('Ocean Blue' exists as granite/marble, scrape says quartzite). A mis-tag would become a phantom, so the
    operator confirms a genuinely new variety (mint) or rejects it. A mint decision on this name un-holds it."""
    types_txt = human_join([title_case(t) for t in existing_types])
    c.pending_confirm.append(review_card("new_type", clean,
        f"'{title_case(clean)}' already exists as {types_txt}, but this scrape is typed "
        f"'{title_case(stone_type)}'. Confirm it is a genuinely NEW variety to mint "
        f"'{title_case(clean)} {title_case(stone_type)}', or reject it as a mis-tag.",
        review_evidence(row), stone_type=title_case(stone_type),
        color=title_case(attr_surface(row, "color")), nearest_existing=named_with_types(c, clean)))


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
