"""Variant and alias curation (the manual-update loop, section 8.3 / 8.4, targeting
the Medusa IMPORT files).

A run surfaces two things the reference is missing:
  - forgotten aliases: a scraped spelling that resolved only via a non-exact tier
    (fuzzy/phonetic) or sits in the review band; it should become an alias of the
    matched variety so next time it is a clean exact hit.
  - new variants: a scraped name that matched nothing; after an alias-vs-new
    decision it may be a genuinely new variety to create.

The system emits additions in the EXACT import-file format (Key,Name,Image,
Aliases) so they can be loaded into Medusa to create/update variants, which then
assigns ids and re-exports the id files the pipeline matches against. It never
rewrites the supplied files; it writes separate curation files for review.

Because a stone is the same material as a slab, block, or tile (only the id and
Key prefix differ), an addition is emitted for every category's import file.
Keys match the existing convention exactly: {branch}_{type}_{name}_{uuid}, where
the uuid is a deterministic uuid5 (stable across runs, structurally a valid uuid).

THE CLEANING / VERIFICATION FLOW spans several stages, each doing its cleaning at the
point in the pipeline where the needed information first exists -- so the order is:
  1. adapter   strip supplier codes + format words from the raw name (clean_variety_name)
  2. normalize resolve type/colour/finish/quality; NAME-over-tag corrects a mis-tagged
               type ('Azul White Quartzite' tagged Onyx -> Quartzite) BEFORE matching, so
               the variety is matched + minted under the correct type
  3. loaders   drop JUNK existing variants (bare-code 'Mgt'; mis-typed under the wrong Key
               type) from the match set, so products never bind to them (they're listed in
               variants_to_delete.csv)
  4. matching  resolve to an existing variant (exact -> projection -> fuzzy -> phonetic)
  5. curate    classify what's still unresolved (the loop below), in STRICT priority order:
               CANONICALISE -> DEDUP -> REJECT/HOLD junk -> RESOLVE to existing -> MINT new.
The single rule for every gate's position: do the cheapest, most DECISIVE thing first, and
REUSE before MINT; when uncertain, HOLD for human review rather than guess.
"""

from __future__ import annotations

import csv
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.config.decisions_store import scope_key
from stone_pipeline.adapters.tokens import clean_variety, strip_format
from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import CATEGORIES, SETTINGS, active_categories, category
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind
from stone_pipeline.core.text import (ascii_fold, looks_code_shaped,
                                      looks_like_artifact as _looks_like_artifact, title_case)
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import ReferenceData, existing_varieties_file, type_slug_from_key
from stone_pipeline.stages.format_resolve import branch_of
from stone_pipeline.stages.normalize import AttributeResolvers

log = logfmt.get_logger("curate")

# deterministic namespace for variant key uuids (fixed, so keys are reproducible)
_KEY_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

# every category derives from the registry.
BRANCHES = tuple(c.name for c in CATEGORIES)


def active_branches() -> tuple[str, ...]:
    """Categories a new variety fans out into: active (Medusa category id set) AND
    fan_out. A category joins automatically once its pcat is set -- no code change.
    A new variety is created in each, but only its product-backed branch gets an
    image (see product_backed below)."""
    return tuple(c.name for c in active_categories() if c.fan_out)


def _slug_us(text: str) -> str:
    """Underscore slug matching the existing Key convention (agata_black). Accent-folded so the
    Key is fold-invariant -- same key whether the caller passes 'Porriño' or 'Porrino' -- matching
    title_case/slugify and so the deterministic Key can never split one variety in two."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", ascii_fold(text or "").casefold())).strip("_")


def gen_key(branch: str, stone_type: str, name: str) -> str:
    """{branch}_{type}_{name}_{uuid}, with a deterministic uuid5 so the same new
    variety yields the same key every run. Empty parts (e.g. a missing stone_type)
    are dropped so the key never has a double underscore (slab__name)."""
    u = uuid.uuid5(_KEY_NS, f"{branch}:{_slug_us(name)}")
    parts = [branch, _slug_us(stone_type), _slug_us(name)]
    return "_".join(p for p in parts if p) + f"_{u}"


def _attr_surface(row, vocab: str) -> str:
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


def _review_evidence(row) -> dict:
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


def _review_card(kind: str, variant: str, reason: str, evidence: dict, *, stone_type: str = "", color: str = "",
                 nearest_existing: str = "", score="", model_prob="") -> dict:
    """One shape for every review-queue card (variants_to_confirm) so the operator queue is uniform no
    matter which hold path built it, and no arm can silently omit a column. `kind` names WHY the card exists
    (new, similar, collision, no_type, new_type, code, retired; the origin queue adds origin):
    it explains, it never limits what the operator may state. `evidence` is _review_evidence()
    (src/scraped/src_url/image/description); its keys never collide with the card fields, so the spread is
    additive."""
    return {"confirm": "", "kind": kind, "variant": variant, "reason": reason, "stone_type": stone_type,
            "color": color, "nearest_existing": nearest_existing, "score": score,
            "model_prob": model_prob, **evidence}


def image_filename(key: str) -> str:
    """The image base name IS the variant Key, so the image-to-variant link is a
    1:1, unambiguous identity (handles duplicate variety names automatically) and
    the same name is used in the variant Image and the backbone image_file. The
    image is no longer part of the tree join (that is by Key now), so the filename
    only needs to identify the variant unambiguously."""
    return f"{key}.png"


def image_url(filename: str) -> str:
    base = SETTINGS.curation.variant_image_base
    return f"{base}{filename}" if base else filename


@dataclass
class ImportFile:
    branch: str
    path: Path | None
    # A variety's identity is (name, TYPE), not name alone: the SAME name legitimately exists as several
    # stones ('Aqua Blue' is a gneiss AND a granite AND a marble AND an onyx). Keying by name alone
    # collapsed them to one arbitrary type (last row wins), so a type-less scrape was silently tied to the
    # wrong type and the mint-dedup went blind to the hidden types. Keep every variety and index by
    # (norm name, norm type); `type` on each variety is derived from its Key (the authority).
    varieties: list[dict] = field(default_factory=list)              # each: {Key,Name,Image,Aliases,Volume,type}
    by_name_type: dict[tuple[str, str], dict] = field(default_factory=dict)  # (norm name, norm type) -> variety
    by_name: dict[str, dict] = field(default_factory=dict)           # norm name -> variety (name-only lookup)


def load_all_existing() -> dict[str, ImportFile]:
    """The EXISTING variants of every category from ONE read of the file the matcher's candidate index is
    built from (`existing_varieties_file`: the live Medusa export, else the committed base), each indexed by
    (normalized Name, normalized stone TYPE). Used to decide alias-vs-new and to dedup. Neither file is written
    by the pipeline, so the catalog is a pure function of (export + scrapes) -- re-running yields the identical
    output. A missing file raises: an empty existing index would mint every known variety again."""
    path = existing_varieties_file()
    imports = {b: ImportFile(branch=b, path=path) for b in BRANCHES}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for r in csv.DictReader(handle):
            name = (r.get("Name") or "").strip()
            key = (r.get("Key") or "").strip()
            imp = imports.get(key.split("_", 1)[0].casefold())     # one combined file; the branch is the Key prefix
            if name and imp is not None:
                v = {
                    "Key": key,
                    "Name": name,
                    "Image": (r.get("Image") or "").strip(),
                    "Aliases": (r.get("Aliases") or "").strip(),
                    "Volume": (r.get("Volume per kg (m³/kg)") or "").strip(),
                    "type": proj.norm(type_slug_from_key(key)),   # the variety's stone type, from its Key
                }
                imp.varieties.append(v)
                imp.by_name_type[(proj.norm(name), v["type"])] = v
                imp.by_name[proj.norm(name)] = v   # name-only lookup (a review suggestion has no type)
    return imports


def load_existing(branch: str) -> ImportFile:
    """One branch's view of load_all_existing (single-branch callers)."""
    return load_all_existing()[branch]


def _alias_list(raw: str) -> list[str]:
    return [a.strip() for a in (raw or "").split("|") if a.strip()]


# The default allowed-finish set for a new variant and the generic fallback colour (for a source that
# supplies none, e.g. zucchi) now live in the active product-domain pack (config/domains/<pack>.yaml); the
# stone pack reproduces the historical values. The human still refines the finish set per the backbone.
# Why a code-shaped name is held for confirmation -- shown in the review 'reason' column (a supplier code
# has no colour, so it never belongs in the colour field). Keyed by looks_code_shaped's return.
def _human_join(items: list[str]) -> str:
    """'A' / 'A and B' / 'A, B, and C' -- a readable list for a review reason string."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _code_reason(code_why: str, base: str) -> str:
    """The review 'reason' for a code-shaped name: says what it looks like AND what to do, naming the
    likely base variety so the operator can alias in one read."""
    kind = ("a trailing grade letter (e.g. 'Rosal C')" if code_why == "lone_letter"
            else "a supplier code")
    lead = f"Looks like {kind}, not a variety name."
    if base:
        return f"{lead} Alias it to '{title_case(base)}' if it is that stone, otherwise reject."
    return f"{lead} Alias it to the real variety if it is a spelling of one, otherwise reject."


# _clean_variety moved to adapters.tokens.clean_variety (the ONE identity derivation, shared with the
# matcher so an operator mint decision keyed by the cleaned name is found for a product too).


def _is_token_subset(short: str, full: str) -> bool:
    """True when every token of `short` is in `full` and `full` is strictly longer
    -- a truncated spelling of an existing variety ('Marjan' vs 'Marjan Silver')."""
    a = {t.casefold() for t in (short or "").split()}
    b = {t.casefold() for t in (full or "").split()}
    return bool(a) and a < b


@dataclass
class CurationResult:
    alias_additions: dict[str, list[dict]] = field(default_factory=dict)   # branch -> rows
    new_variants: dict[str, list[dict]] = field(default_factory=dict)      # branch -> rows
    backbone_new: dict[str, list[dict]] = field(default_factory=dict)      # branch -> posts
    backbone_updates: list[dict] = field(default_factory=list)            # leaf-gap suggestions
    images_to_generate: list[dict] = field(default_factory=list)          # image checklist
    suspicious_names: list[dict] = field(default_factory=list)            # code-like names, NOT minted
    pending_confirm: list[dict] = field(default_factory=list)             # uncertain -> variants_to_confirm.csv
    counts: dict[str, int] = field(default_factory=dict)


def _non_exact_confirmed(row: CanonicalRow) -> bool:
    m = row.variation_method or ""
    # descriptor_* matches recovered the variety from an empty variety_match_key, so the
    # only spelling available is the full raw_name (with its format word) -- not a clean
    # alias. Treat them like exact (no alias proposal) so we do not emit "X Marble Slab".
    return bool(row.variation_id) and not (
        m in ("exact", "override") or m.startswith("exact")
        or m.startswith("projection") or m.startswith("descriptor")
    )


def _alias_model():
    """The tier-7 alias resolver + nearest-variety lookup, trained from the backbones. The model
    itself lives in matching.alias_resolver; this is the curate-side handle."""
    from stone_pipeline.matching.alias_resolver import from_backbones
    return from_backbones()


def _decided(table, source: str, name: str, clean: str):
    """The operator's decision for a row, from a map or set keyed by scope_key: (vendor, spelling) for a
    decision the vendor made for itself, ('', spelling) for what the spelling means to everyone. Vendor first,
    then global; and at each level the SCRAPED spelling first ('bianco white marble', what a statement is keyed
    by), the cleaned identity second ('bianco white', what an older decision is keyed by)."""
    for key in (scope_key(source, name), scope_key(source, clean), scope_key("", name), scope_key("", clean)):
        if isinstance(table, set):
            if key in table:
                return True
        elif key in table:
            return table[key]
    return False if isinstance(table, set) else None


@dataclass
class _Curation:
    """Everything the curation phases share: the inputs read once, the operator's decision maps, and the
    accumulators every phase writes into. One object instead of a dozen closures over a dozen locals."""
    rows: list
    ref: ReferenceData
    imports: dict
    existing_surface: dict          # norm(surface) -> {(owner norm-name, owner norm-type)}
    generic_words: set
    retired_keys: set
    confirm_decisions: dict
    rejected: set
    seed_colors: dict
    seed_types: dict
    seed_names: dict
    seed_scopes: dict
    resolver: object
    near_meta: dict
    alias_floor: float
    valid_type_norms: set
    last_resort_quality: str
    alias_new: dict = field(default_factory=dict)          # (name, type) owner -> {spellings} (confirmed)
    review_candidates: dict = field(default_factory=dict)  # norm name -> {spellings} (needs review)
    pending_confirm: list = field(default_factory=list)
    new_variant_rows: list = field(default_factory=list)
    suspicious: list = field(default_factory=list)
    seen_new: set = field(default_factory=set)             # (norm type, norm name, decision level)
    minted_display: set = field(default_factory=set)       # (norm type, norm display name) minted THIS run
    variety_branches: dict = field(default_factory=dict)   # norm(clean) -> branches a product was seen in
    variety_images: dict = field(default_factory=dict)     # norm(clean) -> first evidence image


def _new_curation(rows: list[CanonicalRow], ref: ReferenceData) -> _Curation:
    imports = load_all_existing()
    resolver, near_meta = _alias_model() if SETTINGS.curation.enable_alias_model else (None, {})
    # E7: a RETIRED variety is not a resolution target -- its name/aliases must NOT be in the surface, or a
    # scrape matching its spelling would resolve onto a retired Key and the product would get a null
    # variation_key and silently never serve. Excluding it means such a scrape mints/holds for review.
    from stone_pipeline.stages import decisions
    retired_keys = decisions.load_retired()
    # every known SURFACE (canonical name + each alias) of an existing variety -> the set of canonical
    # varieties that carry it. A scrape whose cleaned name is already a surface must NEVER mint a duplicate
    # (the alias-aware existence check); unambiguous -> confirm the spelling as an alias, ambiguous -> review.
    # Owners are (name, TYPE) so 'Aqua Blue' resolves to FOUR distinct owners (one per type), not one entry.
    existing_surface: dict[str, set[tuple[str, str]]] = {}
    for b in active_branches():
        for v in imports[b].varieties:
            if v.get("Key") in retired_keys:
                continue
            owner = (proj.norm(v["Name"]), v["type"])
            existing_surface.setdefault(owner[0], set()).add(owner)
            for al in _alias_list(v.get("Aliases", "")):
                existing_surface.setdefault(proj.norm(al), set()).add(owner)
    # vocabulary of pure colour + stone-type words. A name made ONLY of these (e.g. 'Cream Quartzite',
    # 'Black Granite') is an unbranded GENERIC trade name -- its own underdog variety, never to be aliased
    # UP into a premium quarry-specific stone (the backbone lists such generics as aliases of the premium,
    # so the model/surface would otherwise missell it).
    generic_words: set[str] = set()
    for cat in ("color", "type"):
        for canon, _id in ref.attributes.by_category.get(cat, {}).values():
            generic_words |= set(proj.norm(canon).split())
    return _Curation(
        rows=rows, ref=ref, imports=imports, existing_surface=existing_surface, generic_words=generic_words,
        retired_keys=retired_keys,
        # human decisions (the durable decision ledger) + the persistent reject memory
        confirm_decisions=decisions.load_confirm_decisions(),
        rejected=decisions.load_rejected(),
        seed_colors=decisions.load_variety_seed_colors(),   # scope_key -> operator mint colour (over 'Natural')
        seed_types=decisions.load_variety_seed_types(),     # scope_key -> operator-assigned stone type
        seed_names=decisions.load_variety_seed_names(),     # scope_key -> operator-corrected NAME (rename)
        seed_scopes=decisions.load_variety_seed_scopes(),   # scope_key -> that vendor, for a vendor-level mint
        resolver=resolver, near_meta=near_meta,
        alias_floor=SETTINGS.curation.alias_suggest_floor,
        # The canonical Medusa stone types. A scraped type that is not one of these is not a real type (an
        # un-normalized supplier spelling like 'Semiprecious') and must NEVER be minted into a Key -- it
        # holds for review instead. The mint identity gates on it.
        valid_type_norms={proj.norm(t) for t in ref.attributes.canonical_names("type")},
        last_resort_quality=active_pack().last_resort_quality,   # pack default when a mint has no observed quality
    )


def _spelling_of(row: CanonicalRow) -> str:
    """The scraped spelling a decision is keyed on: the match key, else the raw name MINUS its format word
    (so 'Brown Onyx Slab' is 'Brown Onyx' everywhere)."""
    return (row.variety_match_key or strip_format(row.raw_name or "")).strip()


def _level(c: _Curation, src_site: str, name: str, clean: str) -> str:
    """The level a row's mint decision lands on: the vendor (norm) when that vendor decided a stone of its
    own for this spelling (a vendor-level mint, keyed (source, spelling) so another vendor never sees it),
    else '' for the global meaning of the spelling. Two vendors on the global level are ONE identity; a
    vendor on its own level is a separate identity that mints its own variety beside the global one."""
    return proj.norm(_decided(c.seed_scopes, src_site, name, clean) or "")


def variety_identity(c: _Curation, row: CanonicalRow) -> tuple[str, str, str]:
    """The (name, resolved stone_type, cleaned name) a row's variety is minted under -- computed ONE way so
    the dedup, variety_branches, obs_union and the mint all agree on the identity (and its (type, name) key).

    Type precedence is OPERATOR-AUTHORITATIVE, not scraper-authoritative: the operator's MINT decision type
    (seed_type) WINS (a supplier that names a product 'Lumiere Crystal' only SUGGESTS Crystal; an operator
    minting Lumiere as Agate is the authority), else the SCRAPED type (corrected type_name, else raw tag,
    else the gap's suggested type). A non-canonical value at any step is never minted -- the variety stays
    type-less and HOLDS for review, never a garbage-slug Key."""
    mv = [g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation]
    suggested = (mv[0].suggested_type if mv else "") or next(
        (g.suggested_type for g in row.tree_gaps if g.suggested_type), "")
    # the SCRAPE's provisional type -- a suggestion, not an authority. Canonical-gated: a raw supplier
    # spelling that maps to no Medusa type ('Semiprecious') is treated as type-less so it HOLDS.
    scrape_type = row.type_name or row.raw_type or suggested or ""
    if scrape_type and proj.norm(scrape_type) not in c.valid_type_norms:
        scrape_type = ""
    name = _spelling_of(row)
    # clean is derived from the SCRAPE type (never the operator override) so its type token is stripped
    # correctly and the seed lookup key norm(clean) + the Key uuid stay byte-stable run to run -- the
    # operator's type below changes only the final IDENTITY type, never the name/clean/Key.
    clean = clean_variety(name, scrape_type)
    # OPERATOR AUTHORITY: a MINT decision's chosen type overrides the scraper's suggestion; absent a mint
    # type, the scrape type stands (a vendor statement with a type binds through the matcher's scoped-alias
    # tier, so it never reaches here as a type-less gap). Non-canonical operator types are dropped.
    op_mint_type = _decided(c.seed_types, row.src_site, name, clean) or ""
    if op_mint_type and proj.norm(op_mint_type) in c.valid_type_norms:
        stone_type = op_mint_type
    else:
        stone_type = scrape_type or ""
        if stone_type and proj.norm(stone_type) not in c.valid_type_norms:
            stone_type = ""
    return name, stone_type, clean


def _by_name_owner(c: _Curation, nm: str, prefer_type: str = "") -> tuple[str, str] | None:
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
def _collect_matched_aliases(c: _Curation) -> None:
    for row in c.rows:
        spelling = _spelling_of(row)
        if not spelling:
            continue
        if _non_exact_confirmed(row):
            # attach to the variety the row MATCHED, by its (name, Key-type) -- the Key is the authority;
            # fall back to the by-name owner when the match carries no key (defensive / test rows).
            nnm = proj.norm(row.variation_name or "")
            owner = (nnm, proj.norm(type_slug_from_key(row.variation_key))) if row.variation_key \
                else _by_name_owner(c, nnm, row.type_name or row.raw_type or "")
            if owner:
                c.alias_new.setdefault(owner, set()).add(spelling)
        elif (row.variation_method or "") in ("review", "semantic_review", "review_generic"):
            for flag in row.review_flags:
                if flag.field == "variation" and flag.best_guess:
                    c.review_candidates.setdefault(proj.norm(flag.best_guess), set()).add(spelling)


def _observe_branches_and_images(c: _Curation) -> None:
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
        if clean and not c.variety_images.get(k) and (img := _review_evidence(row).get("image")):
            c.variety_images[k] = img


# --- the hold / alias / mint outcomes one gapped row can take -----------------------------------------------
def _collision_owners(c: _Curation, row, gap) -> set[tuple[str, str]]:
    """The (norm name, norm type) owners the MATCHER listed on a collision gap ('Amazon Green Granite' ->
    Amazon Blue, Amazonia, Verde Ubatuba), resolved to their existing types. Empty for any other gap, so
    only the matcher's explicit verdict widens the surface lookup."""
    if not (row.variation_method or "").endswith("_collision") or not gap.nearest_existing:
        return set()
    return {o for n in gap.nearest_existing.split(",")
            for o in c.existing_surface.get(proj.norm(n.strip()), set())}


def _named_with_types(c: _Curation, name: str) -> str:
    """'Name (Type1, Type2)' for the review's nearest-existing column, so the operator sees which type(s)
    a name exists under. Bare name when it exists under none / is unknown here."""
    if not name:
        return ""
    types = sorted({o[1] for o in c.existing_surface.get(proj.norm(name), set())})
    return (f"{title_case(name)} ({_human_join([title_case(t) for t in types])})" if types
            else title_case(name))


def _hold_collision(c: _Curation, clean: str, stone_type: str, owner_names: list[str], row) -> None:
    """ONE surface, SEVERAL varieties: the operator picks which variety it is (globally, or for this vendor
    only), or mints it new. The one review card for every collision, whichever path found it."""
    fam = _human_join([title_case(n) for n in owner_names])
    c.pending_confirm.append(_review_card("collision", clean,
        f"Matches several existing varieties ({fam}). Alias it to the right one, or mint as new.",
        _review_evidence(row), stone_type=stone_type, nearest_existing=fam))


def _hold_for_type(c: _Curation, clean: str, row, cand_types: list[str]) -> None:
    """A type-less scrape whose name is a REAL variety under SEVERAL stone types: hold it for the human to
    assign the correct type, surfacing the candidate types -- never a typeless clone onto an arbitrary type."""
    types_txt = _human_join([title_case(t) for t in cand_types])
    c.pending_confirm.append(_review_card("no_type", clean,
        f"'{title_case(clean)}' already exists as {types_txt}. Pick one of those types to add "
        f"this to the existing variety, or choose a different type to create a new one.",
        _review_evidence(row), color=title_case(_attr_surface(row, "color")),
        nearest_existing=_named_with_types(c, clean)))


def _hold_new_type(c: _Curation, clean: str, stone_type: str, row, existing_types: list[str]) -> None:
    """HOLD-never-guess: the scrape carries a stone type NOT among the types this name already exists under
    ('Ocean Blue' exists as granite/marble, scrape says quartzite). A mis-tag would become a phantom, so the
    operator confirms a genuinely new variety (mint) or rejects it. A mint decision on this name un-holds it."""
    types_txt = _human_join([title_case(t) for t in existing_types])
    c.pending_confirm.append(_review_card("new_type", clean,
        f"'{title_case(clean)}' already exists as {types_txt}, but this scrape is typed "
        f"'{title_case(stone_type)}'. Confirm it is a genuinely NEW variety to mint "
        f"'{title_case(clean)} {title_case(stone_type)}', or reject it as a mis-tag.",
        _review_evidence(row), stone_type=title_case(stone_type),
        color=title_case(_attr_surface(row, "color")), nearest_existing=_named_with_types(c, clean)))


def _hold_retired(c: _Curation, title: str, stone_type: str, obs_color: str, evidence: dict) -> None:
    """Keep-retired-and-surface: a confirmed mint whose deterministic Key lands on a RETIRED variety.
    Retirement is sticky, so neither silently skip it (its rows would gap every run with no signal) nor let
    it slip into the update delta -- surface an explicit un-retire decision instead."""
    c.pending_confirm.append(_review_card("retired", title,
        f"'{title}' was previously RETIRED. Un-retire it to bring it back; it will not be "
        f"re-created otherwise.",
        evidence, stone_type=title_case(stone_type), color=title_case(obs_color or ""),
        nearest_existing=_named_with_types(c, title)))


def _hold_code(c: _Curation, clean: str, code_why: str, base: str, stone_type: str, row) -> None:
    # an UNDECIDED code-shaped name -> hold for review (an operator mint/reject was already honoured)
    c.pending_confirm.append(_review_card("code", clean, _code_reason(code_why, base), _review_evidence(row),
                                          stone_type=stone_type, nearest_existing=_named_with_types(c, base)))


def _hold_similar(c: _Curation, clean: str, stone_type: str, nearest: str, row, gap, prob: float) -> None:
    near_types = sorted({o[1] for o in c.existing_surface.get(proj.norm(nearest), set())})
    type_note = f" ({_human_join([title_case(t) for t in near_types])})" if near_types else ""
    pick_type = " and pick the matching type" if len(near_types) > 1 else ""
    c.pending_confirm.append(_review_card("similar", clean,
        f"Very similar to existing '{title_case(nearest)}'{type_note}. If it is "
        f"the same stone, alias it to '{title_case(nearest)}'{pick_type}. Mint "
        f"as a new variety only if it is genuinely different.",
        _review_evidence(row), stone_type=stone_type, color=gap.suggested_color or "",
        nearest_existing=_named_with_types(c, nearest), score=gap.nearest_score or "",
        model_prob=round(prob, 2)))


def _hold_new(c: _Curation, clean: str, stone_type: str, nearest: str, row, gap) -> None:
    # Per the 'every new variety is reviewed' policy a new variety is a review decision, never a silent
    # auto-mint (an operator 'yes' already minted it, a 'no' already rejected it).
    c.pending_confirm.append(_review_card("new", clean,
        "New variety (no close existing match). Confirm to add it as a new variety, or reject.",
        _review_evidence(row), stone_type=stone_type, color=title_case(_attr_surface(row, "color")),
        nearest_existing=_named_with_types(c, nearest) if nearest else "", score=gap.nearest_score or ""))


def _alias_and_backfill(c: _Curation, owner: tuple[str, str], spelling: str, clean: str, stype: str,
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
                                   title_case(_attr_surface(row, "color")),
                                   (row.quality_name or c.last_resort_quality).strip() or c.last_resort_quality,
                                   title_case(_attr_surface(row, "finish")), gap, backed, _review_evidence(row),
                                   spelling, row.src_site or ""))


def _mint(c: _Curation, clean: str, stone_type: str, row, gap) -> None:
    """Create a NEW variety row (clean + stone_type) -- the last-resort mint, shared with the operator-confirm
    path so a confirmed NEW type on an existing multi-type name mints DIRECTLY.

    MINT + RENAME: a mint decision may carry a corrected NAME (seed_name, keyed by the scraped spelling). The
    variety is then minted under that display name -- Name AND Key -- and the scraped spelling is recorded as
    its alias, so the product binds on the next produce through the alias surface exactly like every alias
    does. Two different spellings renamed to ONE name mint one variety: the second only adds its spelling."""
    name = _spelling_of(row)
    renamed = _decided(c.seed_names, row.src_site, name, clean) or ""
    display = title_case(renamed) if renamed and proj.norm(renamed) != proj.norm(clean) else title_case(clean)
    owner = (proj.norm(display), proj.norm(stone_type))
    if proj.norm(display) != proj.norm(clean) and not _decided(c.seed_scopes, row.src_site, name, clean):
        # the scraped spelling rides onto the mint row via sib_aliases (owner[0] == norm(title)); for a
        # variety that does not exist yet emit_alias_rows finds no import row and skips it, as intended.
        # A rename made FOR one vendor attaches nothing here: that vendor's scoped alias binds its products.
        c.alias_new.setdefault(owner, set()).add(title_case(clean))
    if owner in c.minted_display:
        return                          # a second spelling of the same renamed variety: alias only
    c.minted_display.add(owner)
    c.new_variant_rows.append((
        clean, display, stone_type,
        title_case(_attr_surface(row, "color")),
        (row.quality_name or c.last_resort_quality).strip() or c.last_resort_quality,
        title_case(_attr_surface(row, "finish")), gap,
        c.variety_branches.get(proj.norm(clean), set()),
        _review_evidence(row), name, row.src_site or "",
    ))


# --- phase 2: classify each gapped variety, in STRICT PRIORITY ORDER ----------------------------------------
# The rule for the ordering: do the cheapest, most DECISIVE thing first, and REUSE before MINT.
#   CANONICALISE the name -> DEDUP one decision per identity -> REJECT/HOLD junk (never mint) -> RESOLVE to an
#   EXISTING variety (exact surface, then the fuzzy nearest) -> MINT as the last resort (held for review).
# Uncertain at any resolve/mint step -> HOLD for human review, never guess.
def _classify(c: _Curation) -> None:
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
        if gap is None and _decided(c.confirm_decisions, row.src_site, name, clean) != "yes":
            continue
        # DEDUP: one decision per cleaned identity, keyed on (RESOLVED type, cleaned name, decision level)
        # -- the SAME identity gen_key uses, so two same-named varieties of DIFFERENT types are EACH minted,
        # and a vendor's own stone is a separate identity from the global meaning (see _level).
        identity = (proj.norm(stone_type), proj.norm(clean), _level(c, row.src_site, name, clean))
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
        # falls through to _mint -> the type-less hold at the single enforcement point.
        if stone_type:
            _hold_new(c, clean, stone_type, (gap.nearest_existing or "").split(",")[0].strip(), row, gap)
            continue
        _mint(c, clean, stone_type, row, gap)


def _reject_or_decide(c: _Curation, row, gap, name: str, stone_type: str, clean: str) -> bool:
    """REJECT / HOLD gates (never mint), cheapest + most decisive FIRST, before any reuse-or-mint decision so
    junk can't be matched, aliased, or minted; then the operator's own MINT decision, honoured HERE and
    ONLY here for every row, before any similarity arm can alias or re-review it away. True = handled."""
    if _looks_like_artifact(clean):            # not a variety name at all (code artifact)
        c.suspicious.append({"src_site": row.src_site, "raw_name": name, "cleaned_name": clean})
        return True
    if _decided(c.rejected, row.src_site, name, clean):        # a human said 'no' on a past run
        return True
    if _decided(c.confirm_decisions, row.src_site, name, clean) == "yes":
        # every mint edge (type-less, already-exists, retired) is handled at the single enforcement point
        # in _emit_new_variants, so _mint is safe to call directly
        _mint(c, clean, stone_type, row, gap)
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
        _hold_code(c, clean, code_why, base, stone_type, row)
        return True
    return False


def _is_generic(c: _Curation, clean: str) -> bool:
    ctoks = set(proj.norm(clean).split())
    return bool(ctoks) and ctoks <= c.generic_words


def _resolve_existing(c: _Curation, row, gap, name: str, stone_type: str, clean: str) -> bool:
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
    if _is_generic(c, clean):
        return False
    owners = c.existing_surface.get(proj.norm(clean)) or _collision_owners(c, row, gap)
    if not owners:
        return False
    st = proj.norm(stone_type)
    same_type = sorted(o for o in owners if st and o[1] == st)
    if same_type:
        named = next((o for o in same_type if o[0] == proj.norm(clean)), None)
        if named or len({o[0] for o in same_type}) == 1:
            _alias_and_backfill(c, named or same_type[0], name, clean, stone_type, row, gap)
        else:
            _hold_collision(c, clean, stone_type, sorted({o[0] for o in same_type}), row)
        return True
    if not st:
        # A type the scrape did not make clear is NEVER auto-completed, not even to a single existing
        # same-name pair: (type, variant) is the unique identity; the operator sets the type via review.
        if len({o[0] for o in owners}) == 1:
            _hold_for_type(c, clean, row, sorted({o[1] for o in owners}))
        else:
            _hold_collision(c, clean, "", sorted({o[0] for o in owners}), row)
        return True
    _hold_new_type(c, clean, stone_type, row, sorted({o[1] for o in owners}))
    return True


def _resolve_nearest(c: _Curation, row, gap, name: str, stone_type: str, clean: str) -> bool:
    """RESOLVE against the fuzzy nearest existing variety: the tier-7 model uses name + type + colour to tell
    a real alias ('Marjan' -> 'Marjan Silver') from a distinct sibling ('Cristallo Divine' vs 'Cristallo
    Bianco'): P>=hi auto-confirms the alias, P<=lo mints, the uncertain middle goes to review. Falls back to
    the flat fuzzy floor + token subset when the model isn't available. True = handled."""
    nearest = (gap.nearest_existing or "").split(",")[0].strip()
    if not nearest or _is_generic(c, clean):
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
            _hold_similar(c, clean, stone_type, nearest, row, gap, d.prob)
            return True
        return False                                # verdict "mint" -> a new variety
    if (gap.nearest_score or 0) >= c.alias_floor or _is_token_subset(clean, nearest):
        c.review_candidates.setdefault(proj.norm(nearest), set()).add(name)
        return True
    return False


def _drop_confirmed_from_review(c: _Curation) -> None:
    # A spelling already CONFIRMED as an alias of its real owner must NOT also be proposed as a needs-review
    # alias onto a different fuzzy-near variety -- or an operator could rubber-stamp a wrong merge.
    confirmed = {proj.norm(s) for spset in c.alias_new.values() for s in spset}
    c.review_candidates = {tgt: sp for tgt, sp in ((tgt, {s for s in sp if proj.norm(s) not in confirmed})
                                                   for tgt, sp in c.review_candidates.items()) if sp}


# --- phase 3: emit alias additions -----------------------------------------------------------------------
def _emit_alias_rows(c: _Curation, result: CurationResult) -> None:
    def emit(target, spellings: set[str], confirmed: bool) -> None:
        # target is a (name, type) OWNER (a confirmed alias -> the exact variety) or a bare NAME (a
        # needs-review suggestion, which has no chosen type). Attach to the matching variety per branch.
        for branch in active_branches():
            existing = (c.imports[branch].by_name_type.get(target) if isinstance(target, tuple)
                        else c.imports[branch].by_name.get(target))
            if not existing:
                continue  # variety not in this category's import file
            current = _alias_list(existing["Aliases"])
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


# --- phase 4: emit genuinely-new variants (import row + backbone post + image entry) -----------------------
def _core(key: str, branch: str) -> str:
    return key[len(branch) + 1:].rsplit("_", 1)[0]


def _union_cores(c: _Curation) -> set[str]:
    # A variety belongs to the STONE, not a format: if it exists in ANY active category it exists in all,
    # because the emit's union-fill materializes the row for every category it is missing from. So the
    # "already exists" set is the UNION of cores (type+name slug, branch-independent) across every category:
    # curate never re-mints a variety any category already carries, and a sparse live export (a category
    # Medusa has not fully populated) still counts its varieties via the categories that do carry them.
    cores: set[str] = set()
    for b in BRANCHES:
        cores |= {_core(v["Key"], b) for v in c.imports[b].varieties if v.get("Key")}
    return cores


def _observed_attributes(c: _Curation) -> dict[tuple[str, str], dict[str, set]]:
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


def _emit_new_variants(c: _Curation, result: CurationResult) -> None:
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
            c.pending_confirm.append(_review_card("no_type", title,
                "No stone type detected. Assign the correct type to mint it. "
                "A variety cannot exist without a type.",
                evidence, color=title_case(obs_color or ""),
                nearest_existing=_named_with_types(c, gap.nearest_existing) if gap else ""))
            continue
        # keep-retired-and-surface: a mint whose deterministic Key in ANY fan-out branch is RETIRED was
        # deliberately retired; surface ONE un-retire decision and skip the whole variety (retirement is
        # per-variety, so a retired stone is never resurrected in another format).
        if any(gen_key(b, stone_type, title) in c.retired_keys for b in active_branches()):
            _hold_retired(c, title, stone_type, obs_color, evidence)
            continue
        # observed attributes and the seed colour are looked up by the SCRAPED identity (`name`, the cleaned
        # spelling), not the display title: for a renamed mint a title-keyed lookup would drop them.
        u = obs_union.get((proj.norm(stone_type), proj.norm(name)),
                          {"colors": set(), "qualities": set(), "finishes": set()})
        # colour is REQUIRED for a Medusa product; a source like zucchi often supplies none. An operator-chosen
        # mint colour WINS over the observed/fallback chain (by the scraped spelling, then the cleaned identity;
        # never a colour another vendor's statement chose), else the pack's fallback colour so the variety and
        # its products are always priceable.
        seeded = _decided(c.seed_colors, src, spelling, name)
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
            if _core(key, branch) in union_cores:
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
def _leaf_updates(rows: list[CanonicalRow], result: CurationResult) -> None:
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


def _finish(c: _Curation, result: CurationResult) -> None:
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


def build_curation(rows: list[CanonicalRow], ref: ReferenceData) -> CurationResult:
    """The curation of one produce: what the operator must decide (pending cards), which scraped spellings
    attach to existing varieties (alias additions), and which genuinely new varieties are minted (import
    rows + backbone posts + images to generate). Phases in order; see each function."""
    c = _new_curation(rows, ref)
    _collect_matched_aliases(c)
    _observe_branches_and_images(c)
    _classify(c)
    _drop_confirmed_from_review(c)
    result = CurationResult(
        alias_additions={b: [] for b in BRANCHES},
        new_variants={b: [] for b in BRANCHES},
        backbone_new={b: [] for b in BRANCHES},
    )
    _emit_alias_rows(c, result)
    _emit_new_variants(c, result)
    _leaf_updates(rows, result)
    _finish(c, result)
    return result

def build_attribute_curation(rows: list[CanonicalRow], ref: ReferenceData) -> list[dict]:
    """Colour/finish/type/quality are a CLOSED vocabulary, so an unresolved value
    is almost always a SYNONYM of an existing value (pipeline-side, no Medusa id
    needed), and only rarely a genuinely new value (which goes to the attribute
    file). Aggregate distinct unresolved values per vocab, suggest the nearest
    existing canonical value, and recommend synonym vs new_value."""
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        for flag in row.review_flags:
            if flag.code == FlagCode.attr_unresolved and flag.raw_value:
                key = (flag.field, flag.raw_value.strip())
                seen[key] = seen.get(key, 0) + 1

    # The SAME resolver Stage 3 uses, so the review suggestion never drifts from resolution.
    resolvers = AttributeResolvers.build(ref)
    out: list[dict] = []
    for (vocab, raw_value), count in sorted(seen.items()):
        resolver = resolvers.resolvers.get(vocab)
        if resolver is None:
            continue    # attribute curation only handles the closed attr vocab (type/colour/finish/
                        # quality); a 'variation' unresolved is a variety, owned by the confirm queue
        # resolve() already found the single nearest canonical (evidence); keep it before resolve_multi
        # tries to compose, so the plain-fuzzy fallback reuses that score instead of recomputing it.
        res = resolver.resolve(raw_value)
        nearest = res.evidence or {}
        if res.value is None and res.method != "synonym_none":
            res = resolver.resolve_multi(raw_value)
        if res.value is not None:                       # maps onto an existing value -> adopt as a synonym
            best, action, score = res.value, "synonym", 100.0
        elif res.method == "compound_suggest":          # a real, new compound to create ('A and B')
            best, action, score = res.evidence["best"], "new_value", 90.0
        else:                                           # single unresolved -> resolve()'s fuzzy nearest
            best, score = nearest.get("best") or "", nearest.get("score", 0.0)
            action = "synonym" if score >= 80 else "synonym?" if score >= 60 else "new_value"
        out.append({
            "vocab": vocab, "raw_value": raw_value, "count": count,
            "suggested_value": best, "score": score, "recommended_action": action,
        })
    return out


def write_attribute_curation(attr_rows: list[dict], outputs_dir: Path, run_id: str) -> Path | None:
    if not attr_rows:
        return None
    path = Path(outputs_dir) / f"curation_attributes_{run_id}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["vocab", "raw_value", "count", "suggested_value", "score", "recommended_action"],
        )
        writer.writeheader()
        writer.writerows(attr_rows)
    return path


_IMPORT_COLS = ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]


def _write_csv(path: Path, cols: list[str], rows: list[dict], sanitize: bool = True) -> None:
    # atomic always. sanitize=True (formula-injection-safe) for operator-opened files; sanitize=False
    # for a MEDUSA IMPORT file, where a leading "'" prepended to a Name/Alias would corrupt the data.
    csvio.write_dicts(path, cols, rows, sanitize=sanitize)


def write_curation(result: CurationResult, rows: list[CanonicalRow]) -> None:
    """Write the catalog curation outputs to the fixed top-level folders:
      to_upload/1_variants_update.csv      the new + alias-update DELTA (incremental upload)
      catalog_source/backbone_additions/   new varieties to append to the backbones
      review/                              the human-decision aids (never uploaded)
    The full upload file (to_upload/1_variants_full.csv) is produced by emit_catalog."""
    p = SETTINGS.paths
    to_upload = p.to_upload_dir
    additions = p.catalog_source_dir / "backbone_additions"
    upload_rows: list[dict] = []
    needs_review: list[dict] = []
    triage: list[dict] = []
    for branch in BRANCHES:
        confirmed = [r for r in result.alias_additions[branch] if r.get("_status") == "confirmed"]
        upload_rows += confirmed + result.new_variants[branch]
        needs_review += [r for r in result.alias_additions[branch] if r.get("_status") != "confirmed"]
        triage += result.new_variants[branch]
        # backbone deltas: the new varieties to append to catalog_source/backbone_*.json. ALWAYS
        # (over)write -- including an empty list -- so a variety that is no longer minted this run
        # (resolved, held, or now code-detected like 'Mgt Onyx') can never PERSIST as a stale
        # addition from a prior run (which would keep feeding the image queue + tree).
        bp = additions / f"{branch}.json"
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text(json.dumps(result.backbone_new[branch], indent=2, ensure_ascii=False),
                      encoding="utf-8")
    # Drop no-op rows: a variant whose Key is ALREADY in the export and that adds no new alias and
    # no new image is just upsert noise (the mint guard can miss a duplicate-name generic). Keep the
    # update file to the genuine delta -- new variants + real alias/image changes only.
    exp_idx: dict[str, tuple[set, str]] = {}
    ef = SETTINGS.paths.export_file
    if ef.exists():
        with ef.open(encoding="utf-8-sig") as h:
            for r in csv.DictReader(h):
                if (r.get("Key") or "").strip():
                    exp_idx[r["Key"]] = ({proj.norm(a) for a in (r.get("Aliases") or "").split("|") if a.strip()},
                                         (r.get("Image") or "").strip())

    def _is_real_delta(row: dict) -> bool:
        e = exp_idx.get(row["Key"])
        if e is None:
            return True                                    # genuinely new variant (key not in export)
        cur = {proj.norm(a) for a in (row.get("Aliases") or "").split("|") if a.strip()}
        img = (row.get("Image") or "").strip()
        return bool(cur - e[0]) or (bool(img) and img != e[1])   # new alias OR new image

    # never offer a variant that is flagged for deletion (mis-typed/junk) for (re)loading
    _del = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    _delete_keys: set[str] = set()
    if _del.exists():
        with _del.open(encoding="utf-8-sig") as _handle:   # close the handle (was leaked in a comprehension)
            _delete_keys = {(r.get("Key") or "").strip() for r in csv.DictReader(_handle)}
    before = len(upload_rows)
    upload_rows = [r for r in upload_rows if _is_real_delta(r) and r["Key"] not in _delete_keys]
    if before != len(upload_rows):
        log.info("update delta pruned", extra={"extra_fields": {"dropped_noops": before - len(upload_rows)}})
    if upload_rows:
        _write_csv(to_upload / "1_variants_update.csv", _IMPORT_COLS, upload_rows, sanitize=False)  # Medusa import
    # the ONLY review file from curate: the decision ledger (uncertain new varieties, confirm
    # true/false). Always (re)written so applied/rejected rows drop out. Advisory sets (uncertain
    # alias spellings, code-like names, images) are logged as counts, not clutter files.
    from stone_pipeline.stages import decisions
    decisions.write_confirm_file(result.pending_confirm)
    # SEPARATE origin queue: rows held because a vendor's primary_origin was not corroborated by the map
    # (origin_needs_confirmation). One entry per (source, variety, type); never mixed into the variety queue
    # above. Always called (empty clears it) so a confirmed origin drops off, like the variety queue.
    decisions.write_origin_confirm_file(rows)
    if result.backbone_updates:   # human-readable audit of the leaf additions (the queue below is the surface)
        _write_csv(additions / "backbone_value_updates.csv", decisions.LEAF_COLUMNS, result.backbone_updates)
    # surface the leaf additions for operator review (:4200) -> approve grows the backbone overlay next run.
    # Always called (empty clears the queue) so a resolved suggestion drops off, like the variety queue.
    decisions.write_backbone_leaf_pending(result.backbone_updates)
    result.counts["uncertain_aliases"] = len(needs_review)
    result.counts["suspicious_names_skipped"] = len(result.suspicious_names)
    log.info("curation written", extra={"extra_fields": result.counts})


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
