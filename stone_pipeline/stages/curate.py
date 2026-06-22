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
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, GapKind
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
    """Underscore slug matching the existing Key convention (agata_black)."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", (text or "").casefold())).strip("_")


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


def _title(name: str) -> str:
    return " ".join(w.capitalize() if not w.isupper() else w for w in (name or "").split())


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
    chosen = no_type if len(no_type) >= 2 else no_fmt
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


def build_curation(rows: list[CanonicalRow], ref: ReferenceData) -> CurationResult:
    imports = {b: load_existing(b) for b in BRANCHES}
    result = CurationResult(
        alias_additions={b: [] for b in BRANCHES},
        new_variants={b: [] for b in BRANCHES},
        backbone_new={b: [] for b in BRANCHES},
    )
    alias_floor = SETTINGS.curation.alias_suggest_floor

    # --- 1. collect alias additions: scraped spellings to add to an EXISTING variety
    # variety norm-name -> set of new alias spellings ; confirmed vs needs-review
    from stone_pipeline.adapters.tokens import strip_format
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
        g = next((g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation), None)
        if not g:
            continue
        nm = (row.variety_match_key or row.raw_name or "").strip()
        if nm:
            clean = _clean_variety(nm, row.raw_type or g.suggested_type or "")
            variety_branches.setdefault(proj.norm(clean), set()).add(branch_of(row))

    # --- 2. classify gaps: alias-of-nearest (preferred) vs genuinely new variant --
    seen_new: set[str] = set()
    new_variant_rows: list[tuple] = []  # (name, title, stone_type, obs_color, obs_quality, obs_finish, gap, observed_branches)
    for row in rows:
        gaps = [g for g in row.tree_gaps if g.gap_kind == GapKind.missing_variation]
        if not gaps:
            continue
        name = (row.variety_match_key or row.raw_name or "").strip()
        if not name:
            continue
        gap = gaps[0]
        stone_type = row.raw_type or gap.suggested_type or ""
        clean = _clean_variety(name, stone_type)  # strip format words + stone-type
        # dedup on the MINTED identity (cleaned name), not the raw name: two raw
        # names that clean to the same variety must not mint duplicate Keys.
        if proj.norm(clean) in seen_new:
            continue
        seen_new.add(proj.norm(clean))
        # lean toward alias rather than minting a near-duplicate variant: a decent
        # fuzzy nearest, OR a truncated name that is a token-subset of an existing
        # variety ('Marjan' -> 'Marjan Silver', 'Heser' -> 'Heser Black'). The
        # original spelling is added as the alias.
        nearest = gap.nearest_existing or ""
        if nearest and ((gap.nearest_score or 0) >= alias_floor
                        or _is_token_subset(clean, nearest)):
            review_candidates.setdefault(proj.norm(nearest), set()).add(name)
            continue
        new_variant_rows.append((
            clean, _title(clean), stone_type,
            _title(_attr_surface(row, "color")),
            (row.quality_name or "A").strip() or "A",
            _title(_attr_surface(row, "finish")), gap,
            variety_branches.get(proj.norm(clean), set()),  # product-backed branches
        ))

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
            result.alias_additions[branch].append({
                "Key": existing["Key"], "Name": existing["Name"], "Image": existing["Image"],
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
    for name, title, stone_type, obs_color, obs_quality, obs_finish, gap, observed in new_variant_rows:
        finishes = list(dict.fromkeys([*([obs_finish] if obs_finish else []), *_DEFAULT_FINISHES]))
        for branch in active_branches():
            key = gen_key(branch, stone_type, title)
            if _core(key, branch) in existing_cores[branch]:
                continue  # variety already exists in this branch; do not duplicate it
            fname = image_filename(key)
            product_backed = branch in observed  # a scraped product uses this branch
            result.new_variants[branch].append({
                "Key": key,
                "Name": title,
                "Image": image_url(fname),  # consistent with the backbone image_file
                "Aliases": "",
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
                "color": [obs_color] if obs_color else [],
                "finishes": finishes,
                "qualities": [obs_quality],
                "aliases": [],
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

    # --- 3. backbone updates: existing variety sold in a not-yet-allowed value ---
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
    }
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


def _write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_curation(result: CurationResult) -> None:
    """Write the catalog curation outputs to the fixed top-level folders:
      to_upload/1_variants_update.csv      the new + alias-update DELTA (incremental upload)
      catalog_source/backbone_additions/   new varieties to append to the backbones
      review/                              the human-decision aids (never uploaded)
    The full upload file (to_upload/1_variants_full.csv) is produced by emit_catalog."""
    p = SETTINGS.paths
    to_upload, review = p.to_upload_dir, p.review_dir
    additions = p.catalog_source_dir / "backbone_additions"
    upload_rows: list[dict] = []
    needs_review: list[dict] = []
    triage: list[dict] = []
    for branch in BRANCHES:
        confirmed = [r for r in result.alias_additions[branch] if r.get("_status") == "confirmed"]
        upload_rows += confirmed + result.new_variants[branch]
        needs_review += [r for r in result.alias_additions[branch] if r.get("_status") != "confirmed"]
        triage += result.new_variants[branch]
        # backbone deltas: the new varieties to append to catalog_source/backbone_*.json
        if result.backbone_new.get(branch):
            bp = additions / f"{branch}.json"
            bp.parent.mkdir(parents=True, exist_ok=True)
            bp.write_text(json.dumps(result.backbone_new[branch], indent=2, ensure_ascii=False),
                          encoding="utf-8")
    if upload_rows:
        _write_csv(to_upload / "1_variants_update.csv", _IMPORT_COLS, upload_rows)
    if needs_review:
        _write_csv(review / "alias_candidates.csv", _IMPORT_COLS + ["_added", "_status"], needs_review)
    if triage:
        _write_csv(review / "variants_update_triage.csv",
                   ["Key", "Name", "_suggested_type", "_nearest_existing", "_nearest_score", "_example_url"],
                   triage)
    if result.images_to_generate:
        _write_csv(review / "images_to_generate.csv",
                   ["image_filename", "s3_url", "variant", "category", "status"], result.images_to_generate)
    if result.backbone_updates:
        _write_csv(additions / "backbone_value_updates.csv",
                   ["variety", "attribute", "add_value", "currently_allowed", "match_method",
                    "match_confidence", "verdict", "example_url"], result.backbone_updates)
    log.info("curation written", extra={"extra_fields": result.counts})


def run(rows: list[CanonicalRow], ref: ReferenceData, products_counts: dict | None = None) -> CurationResult:
    result = build_curation(rows, ref)
    if products_counts:
        result.counts.update(products_counts)
    write_curation(result)
    attr = build_attribute_curation(rows, ref)
    if attr:
        _write_csv(SETTINGS.paths.review_dir / "attribute_synonyms.csv",
                   ["vocab", "raw_value", "count", "suggested_value", "score", "recommended_action"], attr)
    result.counts["attribute_values"] = len(attr)
    return result
