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
import html
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import CATEGORIES, SETTINGS, active_categories, category
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, GapKind
from stone_pipeline.core.text import (ascii_fold, looks_code_shaped, looks_codey,
                                      looks_like_artifact as _looks_like_artifact, title_case)
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import ReferenceData, type_slug_from_key
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
            "src_url": getattr(row, "src_url", "") or "",
            "image": next((u for u in imgs if u), ""),
            "description": getattr(row, "description", "") or ""}


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
    path: Path
    present: bool
    # A variety's identity is (name, TYPE), not name alone: the SAME name legitimately exists as several
    # stones ('Aqua Blue' is a gneiss AND a granite AND a marble AND an onyx). Keying by name alone
    # collapsed them to one arbitrary type (last row wins), so a type-less scrape was silently tied to the
    # wrong type and the mint-dedup went blind to the hidden types. Keep every variety and index by
    # (norm name, norm type); `type` on each variety is derived from its Key (the authority).
    varieties: list[dict] = field(default_factory=list)              # each: {Key,Name,Image,Aliases,Volume,type}
    by_name_type: dict[tuple[str, str], dict] = field(default_factory=dict)  # (norm name, norm type) -> variety
    by_name: dict[str, dict] = field(default_factory=dict)           # norm name -> variety (name-only lookup)


def load_existing(branch: str) -> ImportFile:
    """The EXISTING variants of one category, read from the immutable Medusa export (download-only) and
    indexed by (normalized Name, normalized stone TYPE). Used to decide alias-vs-new and to dedup. The
    export is never written by the pipeline, so the catalog is a pure function of (export + scrapes) --
    re-running yields the identical output."""
    path = SETTINGS.paths.export_file
    imp = ImportFile(branch=branch, path=path, present=path.exists())
    if not imp.present:
        return imp
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for r in csv.DictReader(handle):
            name = (r.get("Name") or "").strip()
            key = (r.get("Key") or "").strip()
            # one combined export file; keep only this branch's rows (Key prefix)
            if name and key.casefold().startswith(branch.casefold()):
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
    return imp


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


# format words to strip from a variety name (singular + plural of every category)
_FORMAT_WORDS = {w for c in CATEGORIES for w in (c.name, c.plural)}


def _clean_variety(name: str, stone_type: str) -> str:
    """Suppliers name products '{Variety} {Type} {Format}' (e.g. 'Crystal White
    Granite Slab'). Always strip the format word (slab/block/tile). Strip the
    stone-type token(s) too, but ONLY when a distinctive multi-word name remains
    -- otherwise keep the type so we don't reduce a name to a bare generic colour
    ('Crystal White Granite Slab' -> 'Crystal White', but 'White Onyx Slab' ->
    'White Onyx', not 'White'). Never returns empty."""
    toks = html.unescape(name or "").split()   # decode &#8211; etc. before cleaning
    type_toks = {t.casefold() for t in (stone_type or "").split()}
    no_fmt = [t for t in toks if t.casefold() not in _FORMAT_WORDS]
    no_type = [t for t in no_fmt if t.casefold() not in type_toks]
    # Strip the type when a distinctive multi-word name remains, OR when the single remaining token is
    # a supplier CODE -- so 'MGT Onyx' -> 'MGT' is caught downstream as a bare code and routed to
    # review, NOT minted as a type-baked variety 'Mgt Onyx'. Keep the type only when the remainder is
    # a real word ('White Onyx' -> 'White Onyx', not 'White').
    chosen = no_type if (len(no_type) >= 2 or (len(no_type) == 1 and looks_codey(no_type[0]))) else no_fmt
    # A trailing bare <=2-char token is almost always a supplier lot/region code, not part of the
    # variety name ('White Super ES', ES = Espirito Santo). Drop it so the variety folds to its base
    # name and the full scraped name is kept as an alias -- never minting a separate '... Es' variant.
    # Only when >=2 real tokens remain, so a name is never reduced to a bare colour/type.
    if len(chosen) >= 3 and len(chosen[-1]) <= 2 and chosen[-1].isalnum():
        chosen = chosen[:-1]
    return " ".join(chosen) or " ".join(no_fmt) or (name or "").strip()


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
    rejected: set = field(default_factory=set)                            # user said 'no' -> never propose again
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


def build_curation(rows: list[CanonicalRow], ref: ReferenceData) -> CurationResult:
    imports = {b: load_existing(b) for b in BRANCHES}
    resolver, near_meta = _alias_model() if SETTINGS.curation.enable_alias_model else (None, {})
    # every known SURFACE (canonical name + each alias) of an existing variety -> the set of
    # canonical varieties that carry it. A scrape whose cleaned name is already a surface must
    # NEVER mint a duplicate (this is the alias-aware existence check); unambiguous -> confirm the
    # spelling as an alias, ambiguous across varieties -> review.
    # E7: a RETIRED variety is not a resolution target -- its name/aliases must NOT be in the surface, or a
    # scrape matching its spelling would resolve onto a retired Key and the product would get a null
    # variation_key and silently never serve. Excluding it means such a scrape mints/holds for review.
    from stone_pipeline.stages import decisions as _decisions
    retired_keys = _decisions.load_retired()
    # norm(surface) -> the set of (owner norm-name, owner norm-type) varieties that carry it. Owners are
    # (name, TYPE) so 'Aqua Blue' resolves to FOUR distinct owners (one per type), not one collapsed entry.
    existing_surface: dict[str, set[tuple[str, str]]] = {}
    for b in active_branches():
        for v in imports[b].varieties:
            if v.get("Key") in retired_keys:
                continue
            owner = (proj.norm(v["Name"]), v["type"])
            existing_surface.setdefault(owner[0], set()).add(owner)
            for al in _alias_list(v.get("Aliases", "")):
                existing_surface.setdefault(proj.norm(al), set()).add(owner)
    # vocabulary of pure colour + stone-type words. A name made ONLY of these (e.g. 'Cream
    # Quartzite', 'Black Granite') is an unbranded GENERIC trade name -- its own underdog variety,
    # never to be aliased UP into a premium quarry-specific stone (the backbone lists such generics
    # as aliases of the premium, so the model/surface would otherwise missell it).
    generic_words: set[str] = set()
    for cat in ("color", "type"):
        for canon, _id in ref.attributes.by_category.get(cat, {}).values():
            generic_words |= set(proj.norm(canon).split())
    # human decisions read back from the ledger (variants_to_confirm.csv) + the persistent reject memory
    from stone_pipeline.stages import decisions
    confirm_decisions = decisions.load_confirm_decisions()
    alias_decisions = decisions.load_alias_decisions()   # norm(spelling) -> existing variety NAME to alias onto
    alias_types = decisions.load_alias_types()            # norm(spelling) -> operator-chosen TARGET type (multi-type)
    rejected = decisions.load_rejected()
    seed_colors = decisions.load_variety_seed_colors()   # norm(variant) -> operator mint colour (over 'Natural')
    seed_types = decisions.load_variety_seed_types()     # norm(variant) -> operator-assigned stone type (fills a void)
    pending_confirm: list[dict] = []
    result = CurationResult(
        alias_additions={b: [] for b in BRANCHES},
        new_variants={b: [] for b in BRANCHES},
        backbone_new={b: [] for b in BRANCHES},
    )
    alias_floor = SETTINGS.curation.alias_suggest_floor
    # The canonical Medusa stone types. A scraped type that is not one of these is not a real type (an
    # un-normalized supplier spelling like 'Semiprecious') and must NEVER be minted into a Key -- it holds
    # for review instead. Computed once; the mint identity below gates on it.
    valid_type_norms = {proj.norm(t) for t in ref.attributes.canonical_names("type")}

    # --- 1. collect alias additions: scraped spellings to add to an EXISTING variety
    # variety norm-name -> set of new alias spellings ; confirmed vs needs-review
    from stone_pipeline.adapters.tokens import strip_format

    def variety_identity(row: CanonicalRow) -> tuple[str, str, str]:
        """The (name, resolved stone_type, cleaned name) a row's variety is minted under -- computed
        ONE way so the dedup, variety_branches, obs_union and the mint all agree on the identity (and
        its (type, name) key). Type: the corrected type_name, else the raw tag, else the first
        missing_variation gap's suggested type, else the OPERATOR-ASSIGNED type (seed_type) for a
        type-less variety -- never guessed, and never NON-CANONICAL (see the gate below). Name: the match
        key, else the raw name MINUS its format word (so 'Brown Onyx Slab' is 'Brown Onyx' everywhere)."""
        mv = [g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation]
        suggested = (mv[0].suggested_type if mv else "") or next(
            (g.suggested_type for g in row.tree_gaps if g.suggested_type), "")
        stone_type = row.type_name or row.raw_type or suggested or ""
        # A stone type MUST be a canonical Medusa type. When none of the sources above resolved to one
        # (e.g. a raw supplier spelling 'Semiprecious' that no synonym mapped to 'Semi-Precious Stone'),
        # the value is not a real type -- treat it as type-less so it HOLDS for review (or an operator
        # seed_type fills it below), NEVER minting a variety under a garbage type slug. This validates the
        # RESULT, so a legitimate raw_type that happens to be canonical still passes untouched.
        if stone_type and proj.norm(stone_type) not in valid_type_norms:
            stone_type = ""
        name = (row.variety_match_key or strip_format(row.raw_name or "")).strip()
        # clean is derived from the BASE (pre-seed) type, so the seed only fills the final stone_type and
        # never changes the name/clean/Key uuid -- the seed lookup key norm(clean) stays stable run to run.
        clean = _clean_variety(name, stone_type)
        if not stone_type:
            # Operator-assigned type for a type-less variety: a MINT decision's seed_type, else an ALIAS
            # decision's chosen TARGET type (both keyed by norm(clean)). Reading alias_types here is what
            # lets a 'pick the type' answer on an ALIASED multi-type variety (e.g. Monalisa -> Mona Lisa,
            # a 4-type target) actually resolve the row: with the type filled, PHASE-4a's same_type branch
            # attaches it to the chosen-type owner. Without it the row stays type-less and re-holds forever
            # -- the mint path already read seed_types; the alias path was the missing half of the symmetry.
            stone_type = seed_types.get(proj.norm(clean), "") or alias_types.get(proj.norm(clean), "")
            # the operator type must ALSO be canonical -- the :4200 API validates it, but curate does not
            # trust that boundary: a non-canonical value is dropped so the variety stays type-less (held)
            # rather than minting a garbage-slug Key. Same rule as the scraped-type gate above.
            if stone_type and proj.norm(stone_type) not in valid_type_norms:
                stone_type = ""
        return name, stone_type, clean

    # alias_new / review_candidates now key on the (name, TYPE) OWNER, so a spelling is attached to the
    # correct-type variety -- never collapsed onto an arbitrary same-name variety of a different stone.
    alias_new: dict[tuple[str, str], set[str]] = {}
    review_candidates: dict[str, set[str]] = {}

    def _by_name_owner(nm: str, prefer_type: str = "") -> tuple[str, str] | None:
        """The (name, TYPE) owner for a bare variety NAME (an operator alias target, or a matched row
        missing its variation_key). Resolution, in order:
          1. the ROW's own resolved type disambiguates a multi-type name -- if `prefer_type` is known and a
             (nm, prefer_type) variety exists, use it ('White G' typed Granite aliased to 'Alpine' -> Alpine
             GRANITE, not the also-existing Alpine Marble);
          2. else, if exactly ONE existing variety carries nm as its canonical name -> that one;
          3. else None -- genuinely ambiguous (multi-type name with no type to pick by) -> the caller HOLDS,
             never an arbitrary stone (the cross-type-merge bug).
        Owners are filtered to canonical-name matches so a name that is another variety's ALIAS does not
        falsely make its own canonical owner look ambiguous."""
        same_name = {o for o in existing_surface.get(nm, set()) if o[0] == nm}
        pt = proj.norm(prefer_type)
        if pt and (typed := next((o for o in same_name if o[1] == pt), None)):
            return typed
        return next(iter(same_name)) if len(same_name) == 1 else None

    for row in rows:
        # fall back to the raw name MINUS its format word, so a generic-descriptor
        # spelling is a clean alias ("Brown Onyx") not a junk one ("Brown Onyx Slab").
        spelling = (row.variety_match_key or strip_format(row.raw_name or "")).strip()
        if not spelling:
            continue
        if _non_exact_confirmed(row):
            # attach to the variety the row MATCHED, by its (name, Key-type) -- the Key is the authority;
            # fall back to the by-name owner when the match carries no key (defensive / test rows).
            nnm = proj.norm(row.variation_name or "")
            owner = (nnm, proj.norm(type_slug_from_key(row.variation_key))) if row.variation_key \
                else _by_name_owner(nnm, row.type_name or row.raw_type or "")
            if owner:
                alias_new.setdefault(owner, set()).add(spelling)
        elif (row.variation_method or "") in ("review", "semantic_review", "review_generic"):
            for flag in row.review_flags:
                if flag.field == "variation" and flag.best_guess:
                    review_candidates.setdefault(proj.norm(flag.best_guess), set()).add(spelling)

    # which branches a gapped variety was ACTUALLY observed in (has a scraped
    # product). A variety is still created in every active category for a uniform
    # catalog, but only its product-backed branch needs an image generated -- a
    # slab-only supplier should not trigger a (costly) block image with no product.
    # keyed by the CLEANED variety name (the minted identity), so it lines up with
    # the new-variant dedup below even when two raw names clean to the same variety.
    variety_branches: dict[str, set[str]] = {}
    for row in rows:
        if not any(g.gap_kind == GapKind.missing_variation for g in row.tree_gaps):
            continue
        _, _, clean = variety_identity(row)              # one identity, shared with dedup/obs_union/mint
        if clean:
            variety_branches.setdefault(proj.norm(clean), set()).add(branch_of(row))

    # --- 2. classify each gapped variety, in STRICT PRIORITY ORDER (the cleaning/verification flow).
    # The rule for the ordering: do the cheapest, most DECISIVE thing first, and REUSE before MINT.
    #   PHASE 1  CANONICALISE  the name (_clean_variety, using the RESOLVED type)
    #   PHASE 2  DEDUP         one decision per cleaned identity
    #   PHASE 3  REJECT/HOLD   junk -- artifact, human-rejected, code-shaped -- NEVER mint these
    #   PHASE 4  RESOLVE       to an EXISTING variety (an alias beats a new variant): exact surface
    #                          match first, then the fuzzy nearest (model/floor). A generic trade name
    #                          is its own variety and skips alias routing (never promoted to a premium
    #                          stone).
    #   PHASE 5  MINT          a new variety -- the LAST resort, only for a clean, product-backed name.
    # Uncertain at any resolve/mint step -> HOLD for human review (variants_to_confirm.csv), never guess.
    seen_new: set[tuple[str, str]] = set()  # (norm type, norm name) -- the gen_key identity
    suspicious: list[dict] = []          # names that look like supplier codes, not varieties
    new_variant_rows: list[tuple] = []  # (name, title, stone_type, obs_color, obs_quality, obs_finish, gap, observed_branches, evidence)
    last_resort_quality = active_pack().last_resort_quality   # pack default when a mint has no observed quality

    def _alias_and_backfill(owner: tuple[str, str], spelling: str, clean: str, stype: str, row, gap) -> None:
        """Attach a scraped spelling to an EXISTING variety (by its (name, type) owner). If a product backs
        the variety in a branch where it does NOT yet exist, also mint the missing sibling so the product
        resolves next round (the existing-cores guard skips branches where it already lives)."""
        alias_new.setdefault(owner, set()).add(spelling)
        backed = variety_branches.get(proj.norm(clean), set())
        if backed:
            # Backfill the missing cross-branch sibling of the RESOLVED OWNER, under its OWN canonical name
            # -- never the scraped spelling. Minting under the alias spelling ('Monalisa') gives the sibling
            # a Key core the existing-cores guard cannot match against the owner ('Mona Lisa'), so it
            # duplicates the owner in every branch (the confirmed bug). Using the owner name makes the guard
            # skip every branch where the owner already lives -- a resolved same-typed variety mints NOTHING
            # -- and fills only a genuinely-missing branch, as a true same-named sibling (carrying the scraped
            # spelling as its alias via sib_aliases, which keys on owner[0] == norm(title)).
            owner_name = next((imports[b].by_name_type[owner]["Name"] for b in active_branches()
                               if owner in imports[b].by_name_type), title_case(owner[0]))
            new_variant_rows.append((owner_name, owner_name, stype,
                                     title_case(_attr_surface(row, "color")),
                                     (row.quality_name or last_resort_quality).strip() or last_resort_quality,
                                     title_case(_attr_surface(row, "finish")), gap, backed, _review_evidence(row)))

    def _named_with_types(name: str) -> str:
        """'Name (Type1, Type2)' for the review's nearest-existing column: an existing variety name
        annotated with the stone type(s) it exists under, so the operator sees which type(s) to alias
        into. Bare name when it exists under none / is unknown here."""
        if not name:
            return ""
        types = sorted({o[1] for o in existing_surface.get(proj.norm(name), set())})
        return (f"{title_case(name)} ({_human_join([title_case(t) for t in types])})" if types
                else title_case(name))

    def _hold_for_type(clean: str, row, gap, cand_types: list[str]) -> None:
        """A type-less scrape whose name is a REAL variety under SEVERAL stone types: hold it for the human
        to assign the correct type, surfacing the candidate types -- never a typeless clone onto an
        arbitrary type. (Stage C adds the scraped evidence to help the human decide.)"""
        types_txt = _human_join([title_case(t) for t in cand_types])
        pending_confirm.append({
            "confirm": "", "variant": clean,
            "reason": f"'{title_case(clean)}' already exists as {types_txt}. Pick one of those types to add "
                      f"this to the existing variety, or choose a different type to create a new one.",
            "stone_type": "", "color": title_case(_attr_surface(row, "color")),
            "nearest_existing": _named_with_types(clean),
            "score": "", "model_prob": "", **_review_evidence(row)})

    def _hold_new_type(clean: str, stone_type: str, row, gap, existing_types: list[str]) -> None:
        """BUG6 / HOLD-never-guess: the scrape carries a stone type NOT among the types this name already
        exists under (e.g. 'Ocean Blue' exists as granite/marble, scrape says quartzite). Do not silently
        mint a new-type variety -- a mis-tag would become a phantom. Hold it for the operator to confirm it
        is a genuinely new variety (mint) or a mis-tag. An explicit mint decision on this name un-holds it."""
        types_txt = _human_join([title_case(t) for t in existing_types])
        pending_confirm.append({
            "confirm": "", "variant": clean,
            "reason": f"'{title_case(clean)}' already exists as {types_txt}, but this scrape is typed "
                      f"'{title_case(stone_type)}'. Confirm it is a genuinely NEW variety to mint "
                      f"'{title_case(clean)} {title_case(stone_type)}', or reject it as a mis-tag.",
            "stone_type": title_case(stone_type), "color": title_case(_attr_surface(row, "color")),
            "nearest_existing": _named_with_types(clean),
            "score": "", "model_prob": "", **_review_evidence(row)})

    def _mint(clean: str, stone_type: str, row, gap) -> None:
        """Create a NEW variety row (clean + stone_type) -- the PHASE 5 last-resort mint. Shared with the
        BUG6 confirm path so an operator's confirmed NEW type on an existing multi-type name mints DIRECTLY,
        instead of the fuzzy aliaser absorbing it onto a same-name sibling of a different type."""
        new_variant_rows.append((
            clean, title_case(clean), stone_type,
            title_case(_attr_surface(row, "color")),
            (row.quality_name or last_resort_quality).strip() or last_resort_quality,
            title_case(_attr_surface(row, "finish")), gap,
            variety_branches.get(proj.norm(clean), set()),
            _review_evidence(row),
        ))

    def _apply_operator_alias(clean: str, name: str, row, stone_type: str) -> bool:
        """Route an operator ALIAS decision for `clean` onto its target variety, disambiguated by the
        operator's chosen TARGET type (else the scraped row's own type). Returns True if it aliased OR held
        the row (caller should `continue`), False if there is NO alias decision (caller proceeds). A target
        NAME that exists under several types with no type to pick by is HELD LOUDLY -- naming the types and
        asking which -- never a silent no-op onto an arbitrary same-name stone (the bug an alias to a
        multi-type target like 'Black Sea' = andesite + soapstone otherwise causes)."""
        alias_to = alias_decisions.get(proj.norm(clean))
        if not alias_to:
            return False
        target_type = alias_types.get(proj.norm(clean)) or stone_type
        if own := _by_name_owner(proj.norm(alias_to), target_type):
            alias_new.setdefault(own, set()).add(name)
            return True
        owner_types = sorted({o[1] for o in existing_surface.get(proj.norm(alias_to), set())})
        if owner_types:
            reason = (f"Alias target '{title_case(alias_to)}' exists as "
                      f"{_human_join([title_case(t) for t in owner_types])}. Choose which type to alias "
                      f"'{title_case(clean)}' into (one name can exist under several stone types).")
        else:
            reason = (f"Alias target '{title_case(alias_to)}' is not an existing variety here. "
                      f"Reject '{title_case(clean)}' or choose a real target.")
        pending_confirm.append({
            "confirm": "", "variant": clean, "reason": reason,
            "stone_type": "", "color": "",
            "nearest_existing": _named_with_types(alias_to),
            "score": "", "model_prob": "", **_review_evidence(row)})
        return True

    for row in rows:
        gaps = [g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation]
        if not gaps:
            continue
        gap = gaps[0]
        # PHASE 1 -- CANONICALISE. variety_identity() resolves the name (match key, else raw name minus
        # its format word) and the type (corrected type_name, not the raw tag), so 'Azul White Quartzite'
        # (typed Quartzite) cleans to 'Azul White' and mints as quartzite, not under the wrong 'Onyx' tag.
        name, stone_type, clean = variety_identity(row)
        if not name:
            continue

        # PHASE 2 -- DEDUP: classify each cleaned identity once (two raw names that clean to the same
        # variety must never mint duplicate Keys). Keyed on (RESOLVED type, cleaned name) -- the SAME
        # identity gen_key uses -- so two same-named varieties of DIFFERENT types ('Imperial White'
        # Granite vs Quartzite) are EACH minted with their own Key, not silently collapsed to one by
        # row order (which dropped a whole type non-deterministically).
        identity = (proj.norm(stone_type), proj.norm(clean))
        if identity in seen_new:
            continue
        seen_new.add(identity)

        # PHASE 3 -- REJECT / HOLD gates (never mint), cheapest + most decisive FIRST, before any
        # reuse-or-mint decision so junk can't be matched, aliased, or minted:
        if _looks_like_artifact(clean):            # 3a. not a variety name at all (code artifact)
            suspicious.append({"src_site": row.src_site, "raw_name": name, "cleaned_name": clean})
            continue
        if proj.norm(clean) in rejected:           # 3b. a human said 'no' on a past run
            continue
        # 3c. code-SHAPED names ('Rosal C', 'Trani Bianco H', 'Gs') are supplier codes/grades, not
        # varieties -> NEVER mint them. A trailing lone-letter grade whose de-coded base is a KNOWN,
        # single, NON-colour variety is auto-aliased to that variety ('Rosal C' -> 'Rosal'), so a
        # re-scrape resolves instead of re-adding. The colour guard (base not pure colour/type words)
        # means a colour-named variety is never reclassified or merged ('Agata Black' stays distinct,
        # 'White G' is too bare to auto-resolve -> review). Everything else -> review.
        code_why = looks_code_shaped(clean)
        if code_why == "lone_letter":
            base = re.sub(r"[\s\-]+[A-Za-z]\s*$", "", clean).strip()
            bnorm = proj.norm(base)
            btoks = set(bnorm.split())
            owners = existing_surface.get(bnorm)
            if owners and len(owners) == 1 and btoks and not (btoks <= generic_words):
                alias_new.setdefault(next(iter(owners)), set()).add(name)   # auto-alias to the real variety
                continue
        if code_why:
            base = re.sub(r"[\s\-]+[A-Za-z]\s*$", "", clean).strip() if code_why == "lone_letter" else ""
            if _apply_operator_alias(clean, name, row, stone_type):  # operator: this code is a spelling of X
                continue
            dec = confirm_decisions.get(proj.norm(clean))
            if dec == "no":                            # honour 'no' so the code stops re-appearing
                rejected.add(proj.norm(clean))
                continue
            if dec != "yes":                           # blank -> hold out of the upload and ask
                pending_confirm.append({"confirm": "", "variant": clean,
                                        "reason": _code_reason(code_why, base),
                                        "stone_type": stone_type, "color": "", "nearest_existing": _named_with_types(base),
                                        "score": "", "model_prob": "", **_review_evidence(row)})
                continue
            # dec == "yes" -> the human confirmed it IS a real variety -> fall through to mint
        # PHASE 4 -- RESOLVE to an EXISTING variety (prefer an alias over a new variant). A generic
        # colour+type trade name is its OWN variety, so it skips all alias routing (surface match +
        # model) -- it can never be promoted into a premium named stone (misselling).
        ctoks = set(proj.norm(clean).split())
        generic = bool(ctoks) and ctoks <= generic_words
        # 4a. exact surface match: the cleaned name ALREADY EXISTS -> never mint a duplicate. Owners are
        # (name, TYPE), so a legitimately-multi-type name ('Aqua Blue' = gneiss/granite/marble/onyx) is
        # routed BY TYPE, never collapsed onto an arbitrary same-name variety of a different stone:
        #   * product's type IS an existing type      -> alias to THAT variety
        #   * type-less + exactly one existing variety -> alias to it
        #   * type-less + one name across SEVERAL types -> HOLD for the human to assign the correct type
        #   * a surface shared across several NAMES (an alias family) -> review which variety
        #   * product carries a NEW type (not among the existing) -> fall through to MINT that new variety
        owners = existing_surface.get(proj.norm(clean))
        if owners and not generic:
            st = proj.norm(stone_type)
            same_type = sorted(o for o in owners if st and o[1] == st)
            if same_type:                              # product's type matches an existing variety -> alias
                _alias_and_backfill(same_type[0], name, clean, stone_type, row, gap)
                continue
            if not st:                                 # type-less: decide by how the name resolves
                if len(owners) == 1:                   # exactly one existing variety -> alias to it
                    _alias_and_backfill(next(iter(owners)), name, clean, stone_type, row, gap)
                    continue
                if len({o[0] for o in owners}) == 1:   # SAME name, several TYPES -> hold, assign the type
                    _hold_for_type(clean, row, gap, sorted({o[1] for o in owners}))
                    continue
                # SAME surface across SEVERAL different varieties (an alias family): an uncertain IDENTITY ->
                # the review list, where the operator picks which variety it is (which sets its type) or mints
                # it new. Not a hidden advisory-alias suggestion (which could rubber-stamp a wrong merge): an
                # uncertain identity is a review decision like every other -- one list, nothing filed away.
                fam = _human_join(sorted({title_case(o[0]) for o in owners}))
                pending_confirm.append({"confirm": "", "variant": clean,
                                        "reason": f"Matches several existing varieties ({fam}). Alias it to the "
                                                  f"right one, or mint as new.",
                                        "stone_type": "", "color": "", "nearest_existing": fam,
                                        "score": "", "model_prob": "", **_review_evidence(row)})
                continue
            # st is set but NOT among the existing types -> the scrape claims a NEW stone type on an
            # EXISTING multi-type name. BUG6 / HOLD-never-guess: do not silently mint (a mis-tagged
            # 'Ocean Blue Quartzite' would become a phantom variety). Mint ONLY if the operator explicitly
            # confirmed this name; otherwise hold for them to confirm mint-new-type vs mis-tag.
            if confirm_decisions.get(proj.norm(clean)) != "yes":
                _hold_new_type(clean, stone_type, row, gap, sorted({o[1] for o in owners}))
                continue
            # operator confirmed a genuinely-new type -> MINT it DIRECTLY as that type, and continue. Do NOT
            # fall through to the fuzzy aliaser (4b): the nearest existing variety shares this exact NAME
            # under a DIFFERENT type, so 4b would alias the operator's new-type variety onto that sibling
            # ('Jasper Green Dolomite' -> 'Jasper Green Marble') and silently discard the decision -- the bug
            # behind "I can only mint it as a preset type". The operator is the type authority here; the
            # type-aware existing_cores guard below still prevents a duplicate.
            _mint(clean, stone_type, row, gap)
            continue
        # 4b. fuzzy nearest: alias-vs-new decision against the nearest existing variety. The tier-7
        # model uses name + type + colour to tell a real alias ('Marjan' -> 'Marjan Silver') from a
        # distinct sibling ('Cristallo Divine' vs 'Cristallo Bianco'): P>=hi auto-confirms the alias,
        # P<=lo mints, the uncertain middle goes to review. Falls back to the flat fuzzy floor + token
        # subset when the model isn't available.
        nearest = (gap.nearest_existing or "").split(",")[0].strip()
        if nearest and not generic:
            if resolver is not None:
                nt, nc, nal = near_meta.get(proj.norm(nearest), ("", [], []))
                d = resolver.decide_against(clean, stone_type, [gap.suggested_color or ""],
                                            [nearest, *nal], nt, nc)
                if d.verdict == "alias":
                    # confident -> confirm; attach to the nearest variety of its OWN type (near_meta.nt),
                    # so a spelling lands on the right stone, not an arbitrary same-name one.
                    alias_new.setdefault((proj.norm(nearest), proj.norm(nt)), set()).add(name)
                    continue
                if d.verdict == "review":
                    # uncertain -> the human decides (mint / reject / alias) via the review API
                    if _apply_operator_alias(clean, name, row, stone_type):  # operator: this spelling is variety X
                        continue
                    dec = confirm_decisions.get(proj.norm(clean))
                    if dec == "no":
                        rejected.add(proj.norm(clean))
                        continue
                    if dec != "yes":               # pending: hold out of the upload, ask for a decision
                        near_types = sorted({o[1] for o in existing_surface.get(proj.norm(nearest), set())})
                        type_note = (f" ({_human_join([title_case(t) for t in near_types])})"
                                     if near_types else "")
                        pick_type = " and pick the matching type" if len(near_types) > 1 else ""
                        pending_confirm.append({
                            "confirm": "", "variant": clean,
                            "reason": f"Very similar to existing '{title_case(nearest)}'{type_note}. If it is "
                                      f"the same stone, alias it to '{title_case(nearest)}'{pick_type}. Mint "
                                      f"as a new variety only if it is genuinely different.",
                            "stone_type": stone_type, "color": gap.suggested_color or "",
                            "nearest_existing": _named_with_types(nearest), "score": gap.nearest_score or "",
                            "model_prob": round(d.prob, 2), **_review_evidence(row)})
                        continue
                    # dec == "yes" -> the human confirmed it IS a new variety -> fall through to mint
                # "mint" -> fall through to create a new variant
            elif (gap.nearest_score or 0) >= alias_floor or _is_token_subset(clean, nearest):
                review_candidates.setdefault(proj.norm(nearest), set()).add(name)
                continue
        # PHASE 5 -- a genuinely NEW variety: nothing rejected it, nothing existing claims it. Per the
        # 'every new variety is reviewed' policy, do NOT auto-create it -- HOLD for the operator to confirm
        # (it mints on the next produce), reusing the SAME confirm gate as the code-shaped path so a prior
        # 'yes' mints directly and a 'no' rejects. This is the one list: a new variety is a review decision,
        # never a silent auto-mint. (Cross-branch backfill of an ALREADY-resolved variety still mints via
        # _alias_and_backfill -- that is not a new variety, so it is not gated here.)
        # A TYPE-LESS new variety is NOT gated here: it falls through to _mint -> the type-less hold below
        # ("No stone type detected"), a more specific review ask than this one. Gate only typed new varieties.
        dec = confirm_decisions.get(proj.norm(clean))
        if stone_type and dec == "no":
            rejected.add(proj.norm(clean))
            continue
        if stone_type and dec != "yes":
            pending_confirm.append({"confirm": "", "variant": clean,
                                    "reason": "New variety (no close existing match). Confirm to add it as a "
                                              "new variety, or reject.",
                                    "stone_type": stone_type, "color": title_case(_attr_surface(row, "color")),
                                    "nearest_existing": _named_with_types(nearest) if nearest else "",
                                    "score": gap.nearest_score or "", "model_prob": "", **_review_evidence(row)})
            continue
        _mint(clean, stone_type, row, gap)

    # A spelling already CONFIRMED as an alias of its real owner (gap loop above) must NOT also be
    # proposed as a needs-review alias onto a different fuzzy-near variety (the match-review path's
    # best_guess, e.g. "Marjan Silver Travertine" -> generic "Silver Travertine") -- or an operator
    # could rubber-stamp a wrong merge. Drop confirmed spellings from the review suggestions.
    _confirmed_norm = {proj.norm(s) for spset in alias_new.values() for s in spset}
    review_candidates = {tgt: {s for s in sp if proj.norm(s) not in _confirmed_norm}
                         for tgt, sp in review_candidates.items()}
    review_candidates = {tgt: sp for tgt, sp in review_candidates.items() if sp}

    # --- 3. emit alias additions (after gap classification feeds review_candidates)
    def emit_alias_rows(target, spellings: set[str], confirmed: bool) -> None:
        # target is a (name, type) OWNER (a confirmed alias -> the exact variety) or a bare NAME (a
        # needs-review suggestion, which has no chosen type). Attach to the matching variety per branch.
        for branch in active_branches():
            existing = (imports[branch].by_name_type.get(target) if isinstance(target, tuple)
                        else imports[branch].by_name.get(target))
            if not existing:
                continue  # variety not in this category's import file
            current = _alias_list(existing["Aliases"])
            have = {proj.norm(a) for a in current}
            additions = [s for s in sorted(spellings) if proj.norm(s) not in have]
            if not additions:
                continue
            # re-link the clean deterministic image when the variant has one; never copy
            # the export's empty/internal value (would send a blank Image and risk a wipe)
            img = image_url(image_filename(existing["Key"])) if (existing.get("Image") or "").strip() else ""
            result.alias_additions[branch].append({
                "Key": existing["Key"], "Name": existing["Name"], "Image": img,
                "Aliases": "|".join(current + additions),
                "Volume per kg (m³/kg)": existing.get("Volume") or category(branch).volume_per_kg,
                "_added": "|".join(additions),
                "_status": "confirmed" if confirmed else "needs_review",
            })

    for owner, spellings in alias_new.items():                 # owner = (name, type)
        emit_alias_rows(owner, spellings, confirmed=True)
    for name_norm, spellings in review_candidates.items():     # bare name (no chosen type)
        emit_alias_rows(name_norm, spellings, confirmed=False)

    # Guard against minting a variety that ALREADY EXISTS in a branch (the matcher
    # can miss it -- e.g. a tile product while the tile reference is still empty, or a
    # colour+type generic name). Identity = the gen_key core (type+name slug). For a
    # mirror category (tiles) the existing varieties live in the mirror backbone, not
    # the import file.
    def _core(key: str, branch: str) -> str:
        return key[len(branch) + 1:].rsplit("_", 1)[0]
    # A variety belongs to the STONE, not a format: if it exists in ANY active category it exists in all,
    # because the emit's union-fill materializes the row for every category it is missing from. So a
    # variety's "already exists" set is the UNION of cores across every category (the core -- type+name slug
    # -- is branch-independent). curate therefore never re-mints a variety any category already carries; a
    # genuinely new variety (absent everywhere) is minted into every active branch by the fan-out below.
    # This also survives a sparse live export (a category Medusa has not fully populated): its varieties are
    # still counted via the categories that do carry them, so they are never spuriously re-minted.
    union_cores: set[str] = set()
    for b in BRANCHES:
        union_cores |= {_core(v["Key"], b) for v in imports[b].varieties if v.get("Key")}
    existing_cores: dict[str, set[str]] = {b: union_cores for b in BRANCHES}

    # --- 4. emit genuinely-new variants: import row + backbone post + image entry -
    # A variety is the same material in any format, so it is emitted into every
    # ACTIVE category's import file (the existing backbone is identical across
    # categories). Tiles are excluded until they are wired up (active_branches).
    # Observed attributes per new variety: the UNION across EVERY product that maps to it, not just
    # the one row that triggered the mint. Otherwise a variety is born with one product's colour/
    # finish/quality and leaf-gaps every sibling that differs (Azzurra Bay), or gets an empty colour
    # set when its trigger product had none -- nulling colour_id for all its products (Antibes).
    obs_union: dict[tuple[str, str], dict[str, set]] = {}   # (norm type, norm name) -> observed attrs
    for _r in rows:
        # SAME identity as the dedup/mint (variety_identity), so a row whose attrs we want to union
        # keys to exactly the variety that gets minted -- else the variety is born with the default
        # fallback instead of its observed colours/finishes/qualities.
        _nm, _st, _clean = variety_identity(_r)
        if not _nm:
            continue
        _k = (proj.norm(_st), proj.norm(_clean))
        u = obs_union.setdefault(_k, {"colors": set(), "qualities": set(), "finishes": set()})
        if _r.color_name:
            u["colors"].add(title_case(_r.color_name))
        if _r.quality_name:
            u["qualities"].add(_r.quality_name.strip())
        if _r.finish_name:
            u["finishes"].add(title_case(_r.finish_name))

    for name, title, stone_type, obs_color, obs_quality, obs_finish, gap, observed, evidence in new_variant_rows:
        if not stone_type:
            # a variety cannot mint without a stone type (it drives the Key, so a wrong/empty type is a
            # wrong identity). HOLD it for the operator to assign one via the review (seed_type) instead
            # of guessing or shipping it type-less. This is the single enforcement point for the invariant.
            # Carry the scraped evidence (src/image/description) so the human can judge the type.
            pending_confirm.append({"confirm": "", "variant": title,
                                    "reason": "No stone type detected. Assign the correct type to mint it. "
                                              "A variety cannot exist without a type.",
                                    "stone_type": "", "color": title_case(obs_color or ""),
                                    "nearest_existing": _named_with_types(gap.nearest_existing) if gap else "",
                                    "score": "", "model_prob": "", **evidence})
            continue
        _u = obs_union.get((proj.norm(stone_type), proj.norm(title)),
                           {"colors": set(), "qualities": set(), "finishes": set()})
        # colour is REQUIRED for a Medusa product; a source like zucchi often supplies none, so a
        # variety would be born colourless and null every product's colour_id. Fall back to the
        # generic 'Multicolor' (a real attribute) so the variety + its products are always priceable.
        # an operator-chosen mint colour (from the new-variety review) WINS over the observed/fallback
        # chain, so a colourless source is seeded with a real colour instead of the generic 'Natural'.
        seeded = seed_colors.get(proj.norm(title))
        _pack = active_pack()
        colors = ([seeded] if seeded
                  else sorted(_u["colors"]) or ([obs_color] if obs_color else []) or [_pack.fallback_color])
        qualities = sorted(_u["qualities"]) or [obs_quality]
        finishes = list(dict.fromkeys([*sorted(_u["finishes"]),
                                       *([obs_finish] if obs_finish else []), *_pack.default_finishes]))
        # A cross-branch sibling's variety already exists in another branch carrying the scraped
        # spelling as an alias (alias_new). Carry that SAME alias onto the new sibling so its product
        # resolves in ONE upload, not two (mint the variety AND attach its alias in the same leg --
        # otherwise a brand-new branch like a block needs a second round-trip to add the alias).
        sib_aliases = sorted({s for owner, sp in alias_new.items()
                              if owner[0] == proj.norm(title) for s in sp})
        for branch in active_branches():
            key = gen_key(branch, stone_type, title)
            if _core(key, branch) in existing_cores[branch]:
                continue  # variety already exists in this branch; do not duplicate it
            fname = image_filename(key)
            product_backed = branch in observed  # a scraped product uses this branch
            result.new_variants[branch].append({
                "Key": key,
                "Name": title,
                # only link an image when a product uses this branch; fan-out tile/block
                # copies stay imageless until a product adds them (lazy generation)
                "Image": image_url(fname) if product_backed else "",
                "Aliases": "|".join(sib_aliases),
                "Volume per kg (m³/kg)": category(branch).volume_per_kg,
                "_nearest_existing": gap.nearest_existing or "",
                "_nearest_score": gap.nearest_score if gap.nearest_score is not None else "",
                "_suggested_type": stone_type,
                "_example_url": gap.example_src_url or "",
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
            # only list an image to generate when a product actually uses this branch;
            # fan-out copies with no product are skipped (no wasted generation cost)
            if product_backed:
                result.images_to_generate.append({
                    "image_filename": fname,
                    "s3_url": image_url(fname),
                    "variant": title,
                    "category": category(branch).label,
                    "status": "to_generate",
                })

    # --- 5. backbone updates: existing variety sold in a not-yet-allowed value ---
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
                    "stone_type": row.type_name or "",   # disambiguates same-named varieties (granite vs quartzite)
                    "attribute": attribute,
                    "add_value": value,
                    "currently_allowed": g.nearest_existing or "",
                    "match_method": row.variation_method or "",
                    "match_confidence": row.variation_confidence or "",
                    "verdict": "likely_real" if (row.variation_method or "").startswith(("exact", "projection"))
                    else "verify_match",
                    "example_url": g.example_src_url or "",
                })

    result.counts = {
        "alias_additions": sum(len(v) for v in result.alias_additions.values()),
        "confirmed_alias_additions": sum(1 for v in result.alias_additions.values()
                                         for r in v if r.get("_status") == "confirmed"),
        "new_variants": sum(len(v) for v in result.new_variants.values()),
        "distinct_new_varieties": len(new_variant_rows),
        "backbone_updates": len(result.backbone_updates),
        "images_to_generate": len(result.images_to_generate),
        "suspicious_names": len(suspicious),
        "pending_confirm": len(pending_confirm),
    }
    result.suspicious_names = suspicious   # written to review by write_curation
    result.pending_confirm = pending_confirm
    result.rejected = rejected
    # Defence in depth: the mint-emission HOLD makes a type-less variety impossible here; surface it LOUD
    # if a future change ever regresses, rather than shipping a type-less (bad-Key) variety to Medusa.
    typeless = [p["variant"] for posts in result.backbone_new.values() for p in posts if not p.get("stone_type")]
    if typeless:
        log.error("type-less varieties reached the mint; should have been HELD for seed_type",
                  extra={"extra_fields": {"count": len(typeless), "sample": typeless[:5]}})
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


def write_curation(result: CurationResult) -> None:
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
    decisions.save_rejected(result.rejected)
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
    write_curation(result)
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
    return result
