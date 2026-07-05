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

from stone_pipeline.config.settings import CATEGORIES, SETTINGS, active_categories, category
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.schema import CanonicalRow, GapKind
from stone_pipeline.core.text import (ascii_fold, looks_code_shaped, looks_codey,
                                      looks_like_artifact as _looks_like_artifact, title_case)
from stone_pipeline.matching import projections as proj
from stone_pipeline.reference.loaders import ReferenceData
from stone_pipeline.stages.format_resolve import branch_of

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
    Key is fold-invariant — same key whether the caller passes 'Porriño' or 'Porrino' — matching
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
    by_name: dict[str, dict] = field(default_factory=dict)  # norm(name) -> {Key,Name,Image,Aliases}


def load_existing(branch: str) -> ImportFile:
    """The EXISTING variants of one category, keyed by normalized Name, read from the
    immutable Medusa export (download-only). Used to decide alias-vs-new and to dedup.
    The export is never written by the pipeline, so the catalog is a pure function of
    (export + scrapes) -- re-running yields the identical output."""
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
                imp.by_name[proj.norm(name)] = {
                    "Key": (r.get("Key") or "").strip(),
                    "Name": name,
                    "Image": (r.get("Image") or "").strip(),
                    "Aliases": (r.get("Aliases") or "").strip(),
                    "Volume": (r.get("Volume per kg (m³/kg)") or "").strip(),
                }
    return imp


def _alias_list(raw: str) -> list[str]:
    return [a.strip() for a in (raw or "").split("|") if a.strip()]


# a sensible default allowed-finish set for a new variant; the human refines it
# (the backbone dictates which combinations a product may be uploaded as)
_DEFAULT_FINISHES = ["Polished", "Honed", "Leathered", "Brushed", "Flamed",
                     "Sandblasted", "Sawn Cut", "Raw"]
# Generic fallback colour for a variety whose source supplies none (e.g. zucchi). Keeps the variety
# + its products priceable. Must exist as a colour attribute in Medusa (see attributes_to_add.csv).
_FALLBACK_COLOR = "Natural"


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
    existing_surface: dict[str, set[str]] = {}
    for b in active_branches():
        for nrm, v in imports[b].by_name.items():
            if v.get("Key") in retired_keys:
                continue
            existing_surface.setdefault(nrm, set()).add(nrm)
            for al in _alias_list(v.get("Aliases", "")):
                existing_surface.setdefault(proj.norm(al), set()).add(nrm)
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
    rejected = decisions.load_rejected()
    pending_confirm: list[dict] = []
    result = CurationResult(
        alias_additions={b: [] for b in BRANCHES},
        new_variants={b: [] for b in BRANCHES},
        backbone_new={b: [] for b in BRANCHES},
    )
    alias_floor = SETTINGS.curation.alias_suggest_floor

    # --- 1. collect alias additions: scraped spellings to add to an EXISTING variety
    # variety norm-name -> set of new alias spellings ; confirmed vs needs-review
    from stone_pipeline.adapters.tokens import strip_format

    def variety_identity(row: CanonicalRow) -> tuple[str, str, str]:
        """The (name, resolved stone_type, cleaned name) a row's variety is minted under -- computed
        ONE way so the dedup, variety_branches, obs_union and the mint all agree on the identity (and
        its (type, name) key). Type: the corrected type_name, else the raw tag, else the first
        missing_variation gap's suggested type (then any gap's). Name: the match key, else the raw
        name MINUS its format word (so 'Brown Onyx Slab' is 'Brown Onyx' everywhere, not just at mint)."""
        mv = [g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation]
        suggested = (mv[0].suggested_type if mv else "") or next(
            (g.suggested_type for g in row.tree_gaps if g.suggested_type), "")
        stone_type = row.type_name or row.raw_type or suggested or ""
        name = (row.variety_match_key or strip_format(row.raw_name or "")).strip()
        return name, stone_type, _clean_variety(name, stone_type)

    alias_new: dict[str, set[str]] = {}
    review_candidates: dict[str, set[str]] = {}
    for row in rows:
        # fall back to the raw name MINUS its format word, so a generic-descriptor
        # spelling is a clean alias ("Brown Onyx") not a junk one ("Brown Onyx Slab").
        spelling = (row.variety_match_key or strip_format(row.raw_name or "")).strip()
        if not spelling:
            continue
        if _non_exact_confirmed(row):
            alias_new.setdefault(proj.norm(row.variation_name or ""), set()).add(spelling)
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
    new_variant_rows: list[tuple] = []  # (name, title, stone_type, obs_color, obs_quality, obs_finish, gap, observed_branches)
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
            dec = confirm_decisions.get(proj.norm(clean))
            if dec == "no":                            # honour 'no' so the code stops re-appearing
                rejected.add(proj.norm(clean))
                continue
            if dec != "yes":                           # blank -> hold out of the upload and ask
                pending_confirm.append({"confirm": "", "variant": clean, "stone_type": stone_type,
                                        "color": code_why, "nearest_existing": base, "score": "", "model_prob": ""})
                continue
            # dec == "yes" -> the human confirmed it IS a real variety -> fall through to mint
        # PHASE 4 -- RESOLVE to an EXISTING variety (prefer an alias over a new variant). A generic
        # colour+type trade name is its OWN variety, so it skips all alias routing (surface match +
        # model) -- it can never be promoted into a premium named stone (misselling).
        ctoks = set(proj.norm(clean).split())
        generic = bool(ctoks) and ctoks <= generic_words
        # 4a. exact surface match: the cleaned name ALREADY EXISTS as a variety (any branch/type) ->
        # never mint a duplicate. A typeless source whose name collides with an existing variety
        # (possibly ambiguous across types) is routed to review so a human confirms which variety it
        # is, instead of auto-cloning a typeless copy.
        owners = existing_surface.get(proj.norm(clean))
        if owners and not generic:
            if len(owners) == 1:                       # unambiguous existing variety (name or alias)
                alias_new.setdefault(next(iter(owners)), set()).add(name)
                # If a scraped product backs this variety in a branch where it does NOT yet exist
                # (e.g. a block product for a variety that only has a slab/tile sibling), ALSO mint
                # the missing sibling so the product can resolve next round -- otherwise the alias
                # dead-ends (emit_alias_rows can only attach where the variety already exists) and the
                # product never gets a variation_id. Gated on an ALREADY-EXISTING owner + a real
                # product, so it can only ever create a sibling of a KNOWN variety, never a junk mint;
                # the new-variant emitter's existing-cores guard skips the branches where it lives.
                # (clean is already in seen_new from the dedup above, so the loop runs this once per
                # variety; the new-variant emitter then skips the branches where it already exists.)
                backed = variety_branches.get(proj.norm(clean), set())
                if backed:
                    new_variant_rows.append((
                        clean, title_case(clean), stone_type,
                        title_case(_attr_surface(row, "color")),
                        (row.quality_name or "A").strip() or "A",
                        title_case(_attr_surface(row, "finish")), gap, backed))
            else:                                       # the surface spans several varieties/types
                review_candidates.setdefault(proj.norm(clean), set()).add(name)
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
                    alias_new.setdefault(proj.norm(nearest), set()).add(name)   # confident -> confirmed
                    continue
                if d.verdict == "review":
                    # uncertain -> the human decides via variants_to_confirm.csv
                    dec = confirm_decisions.get(proj.norm(clean))
                    if dec == "no":
                        rejected.add(proj.norm(clean))
                        continue
                    if dec != "yes":               # pending: hold out of the upload, ask for a decision
                        pending_confirm.append({
                            "confirm": "", "variant": clean, "stone_type": stone_type,
                            "color": gap.suggested_color or "", "nearest_existing": nearest,
                            "score": gap.nearest_score or "", "model_prob": round(d.prob, 2)})
                        continue
                    # dec == "yes" -> the human confirmed it IS a new variety -> fall through to mint
                # "mint" -> fall through to create a new variant
            elif (gap.nearest_score or 0) >= alias_floor or _is_token_subset(clean, nearest):
                review_candidates.setdefault(proj.norm(nearest), set()).add(name)
                continue
        # PHASE 5 -- MINT a new variety: nothing rejected it, nothing existing claims it -> it is a
        # clean, novel, product-backed name, so create it (the last resort).
        new_variant_rows.append((
            clean, title_case(clean), stone_type,
            title_case(_attr_surface(row, "color")),
            (row.quality_name or "A").strip() or "A",
            title_case(_attr_surface(row, "finish")), gap,
            variety_branches.get(proj.norm(clean), set()),  # product-backed branches
        ))

    # A spelling already CONFIRMED as an alias of its real owner (gap loop above) must NOT also be
    # proposed as a needs-review alias onto a different fuzzy-near variety (the match-review path's
    # best_guess, e.g. "Marjan Silver Travertine" -> generic "Silver Travertine") -- or an operator
    # could rubber-stamp a wrong merge. Drop confirmed spellings from the review suggestions.
    _confirmed_norm = {proj.norm(s) for spset in alias_new.values() for s in spset}
    review_candidates = {tgt: {s for s in sp if proj.norm(s) not in _confirmed_norm}
                         for tgt, sp in review_candidates.items()}
    review_candidates = {tgt: sp for tgt, sp in review_candidates.items() if sp}

    # --- 3. emit alias additions (after gap classification feeds review_candidates)
    def emit_alias_rows(name_norm: str, spellings: set[str], confirmed: bool) -> None:
        for branch in active_branches():
            existing = imports[branch].by_name.get(name_norm)
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

    for name_norm, spellings in alias_new.items():
        emit_alias_rows(name_norm, spellings, confirmed=True)
    for name_norm, spellings in review_candidates.items():
        emit_alias_rows(name_norm, spellings, confirmed=False)

    # Guard against minting a variety that ALREADY EXISTS in a branch (the matcher
    # can miss it -- e.g. a tile product while the tile reference is still empty, or a
    # colour+type generic name). Identity = the gen_key core (type+name slug). For a
    # mirror category (tiles) the existing varieties live in the mirror backbone, not
    # the import file.
    def _core(key: str, branch: str) -> str:
        return key[len(branch) + 1:].rsplit("_", 1)[0]
    existing_cores: dict[str, set[str]] = {}
    for b in BRANCHES:
        cat = category(b)
        if cat.mirror_of:
            try:
                posts = json.loads(cat.backbone_path.read_text(encoding="utf-8-sig")).get("posts", [])
                existing_cores[b] = {_core(p["key"], b) for p in posts if p.get("key")}
            except FileNotFoundError:
                existing_cores[b] = set()
        else:
            existing_cores[b] = {_core(v["Key"], b) for v in imports[b].by_name.values() if v.get("Key")}

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

    for name, title, stone_type, obs_color, obs_quality, obs_finish, gap, observed in new_variant_rows:
        _u = obs_union.get((proj.norm(stone_type), proj.norm(title)),
                           {"colors": set(), "qualities": set(), "finishes": set()})
        # colour is REQUIRED for a Medusa product; a source like zucchi often supplies none, so a
        # variety would be born colourless and null every product's colour_id. Fall back to the
        # generic 'Multicolor' (a real attribute) so the variety + its products are always priceable.
        colors = sorted(_u["colors"]) or ([obs_color] if obs_color else []) or [_FALLBACK_COLOR]
        qualities = sorted(_u["qualities"]) or [obs_quality]
        finishes = list(dict.fromkeys([*sorted(_u["finishes"]),
                                       *([obs_finish] if obs_finish else []), *_DEFAULT_FINISHES]))
        # A cross-branch sibling's variety already exists in another branch carrying the scraped
        # spelling as an alias (alias_new). Carry that SAME alias onto the new sibling so its product
        # resolves in ONE upload, not two (mint the variety AND attach its alias in the same leg --
        # otherwise a brand-new branch like a block needs a second round-trip to add the alias).
        sib_aliases = sorted({s for owner, sp in alias_new.items()
                              if proj.norm(owner) == proj.norm(title) for s in sp})
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
    return result


def build_attribute_curation(rows: list[CanonicalRow], ref: ReferenceData) -> list[dict]:
    """Colour/finish/type/quality are a CLOSED vocabulary, so an unresolved value
    is almost always a SYNONYM of an existing value (pipeline-side, no Medusa id
    needed), and only rarely a genuinely new value (which goes to the attribute
    file). Aggregate distinct unresolved values per vocab, suggest the nearest
    existing canonical value, and recommend synonym vs new_value."""
    from rapidfuzz import fuzz

    from stone_pipeline.core.schema import FlagCode

    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        for flag in row.review_flags:
            if flag.code == FlagCode.attr_unresolved and flag.raw_value:
                key = (flag.field, flag.raw_value.strip())
                seen[key] = seen.get(key, 0) + 1

    out: list[dict] = []
    for (vocab, raw_value), count in sorted(seen.items()):
        canon = ref.attributes.canonical_names(vocab)
        best, best_score = "", 0.0
        for value in canon:
            s = max(fuzz.ratio(proj.norm(raw_value), proj.norm(value)),
                    fuzz.token_sort_ratio(proj.norm(raw_value), proj.norm(value)))
            if s > best_score:
                best, best_score = value, s
        action = "synonym" if best_score >= 80 else ("synonym?" if best_score >= 60 else "new_value")
        out.append({
            "vocab": vocab, "raw_value": raw_value, "count": count,
            "suggested_value": best, "score": round(best_score, 1),
            "recommended_action": action,
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
    if result.backbone_updates:   # value changes to apply to the backbones (apply artifact, not review)
        _write_csv(additions / "backbone_value_updates.csv",
                   ["variety", "attribute", "add_value", "currently_allowed", "match_method",
                    "match_confidence", "verdict", "example_url"], result.backbone_updates)
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
