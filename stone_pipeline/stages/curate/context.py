"""The curation context: everything the phases share (inputs read once, the operator's decision maps, the
accumulators every phase writes into), the result they fill, and the ONE identity derivation every phase
keys on -- so the dedup, the observed attributes, the alias owners and the mint all agree on a variety."""

from __future__ import annotations

from dataclasses import dataclass, field

from stone_pipeline.adapters.tokens import clean_variety, strip_format
from stone_pipeline.config.decisions_store import scope_key
from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import ReferenceData
from stone_pipeline.stages.curate.existing import alias_list
from stone_pipeline.stages.curate.keys import active_branches


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


def alias_model():
    """The tier-7 alias resolver + nearest-variety lookup, trained from the backbones. The model itself lives
    in matching.alias_resolver; this is the curate-side handle. Tests replace it on the curate PACKAGE
    (`curate._alias_model`), so new_curation resolves it through the package at call time."""
    from stone_pipeline.matching.alias_resolver import from_backbones
    return from_backbones()


def decided(table, source: str, name: str, clean: str):
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
class Curation:
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


def new_curation(rows: list[CanonicalRow], ref: ReferenceData) -> Curation:
    # The two inputs a curation is built from -- the existing-variety index and the alias model -- are
    # resolved through the PACKAGE at call time: the package facade is the one seam an operator harness or a
    # test replaces (`curate.load_all_existing`, `curate._alias_model`), so the replacement reaches here.
    from stone_pipeline.stages import curate as pkg
    imports = pkg.load_all_existing()
    resolver, near_meta = pkg._alias_model() if SETTINGS.curation.enable_alias_model else (None, {})
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
            for al in alias_list(v.get("Aliases", "")):
                existing_surface.setdefault(proj.norm(al), set()).add(owner)
    # vocabulary of pure colour + stone-type words. A name made ONLY of these (e.g. 'Cream Quartzite',
    # 'Black Granite') is an unbranded GENERIC trade name -- its own underdog variety, never to be aliased
    # UP into a premium quarry-specific stone (the backbone lists such generics as aliases of the premium,
    # so the model/surface would otherwise missell it).
    generic_words: set[str] = set()
    for cat in ("color", "type"):
        for canon, _id in ref.attributes.by_category.get(cat, {}).values():
            generic_words |= set(proj.norm(canon).split())
    return Curation(
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


def spelling_of(row: CanonicalRow) -> str:
    """The scraped spelling a decision is keyed on: the match key, else the raw name MINUS its format word
    (so 'Brown Onyx Slab' is 'Brown Onyx' everywhere)."""
    return (row.variety_match_key or strip_format(row.raw_name or "")).strip()


def level(c: Curation, src_site: str, name: str, clean: str) -> str:
    """The level a row's mint decision lands on: the vendor (norm) when that vendor decided a stone of its
    own for this spelling (a vendor-level mint, keyed (source, spelling) so another vendor never sees it),
    else '' for the global meaning of the spelling. Two vendors on the global level are ONE identity; a
    vendor on its own level is a separate identity that mints its own variety beside the global one."""
    return proj.norm(decided(c.seed_scopes, src_site, name, clean) or "")


def variety_identity(c: Curation, row: CanonicalRow) -> tuple[str, str, str]:
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
    name = spelling_of(row)
    # clean is derived from the SCRAPE type (never the operator override) so its type token is stripped
    # correctly and the seed lookup key norm(clean) + the Key uuid stay byte-stable run to run -- the
    # operator's type below changes only the final IDENTITY type, never the name/clean/Key.
    clean = clean_variety(name, scrape_type)
    # OPERATOR AUTHORITY: a MINT decision's chosen type overrides the scraper's suggestion; absent a mint
    # type, the scrape type stands (a vendor statement with a type binds through the matcher's scoped-alias
    # tier, so it never reaches here as a type-less gap). Non-canonical operator types are dropped.
    op_mint_type = decided(c.seed_types, row.src_site, name, clean) or ""
    if op_mint_type and proj.norm(op_mint_type) in c.valid_type_norms:
        stone_type = op_mint_type
    else:
        stone_type = scrape_type or ""
        if stone_type and proj.norm(stone_type) not in c.valid_type_norms:
            stone_type = ""
    return name, stone_type, clean


def is_generic(c: Curation, clean: str) -> bool:
    """A name made ONLY of colour + type words is an unbranded generic trade name: its own variety."""
    ctoks = set(proj.norm(clean).split())
    return bool(ctoks) and ctoks <= c.generic_words


def is_token_subset(short: str, full: str) -> bool:
    """True when every token of `short` is in `full` and `full` is strictly longer
    -- a truncated spelling of an existing variety ('Marjan' vs 'Marjan Silver')."""
    a = {t.casefold() for t in (short or "").split()}
    b = {t.casefold() for t in (full or "").split()}
    return bool(a) and a < b
