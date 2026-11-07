"""Build the valid-combination set for Medusa's relational `valid_combination` table.

FULL combination set (the relational table has no size ceiling, so we pre-load every
possible combination -- a future product is then far more likely to already match,
needing no rebuild). Each variation gets EVERY finish its category supports x the UNION
of every colour/quality we know for the SAME (type, variety): its backbone post, the scraped
product, and a same-(type, variety) product in another category. The type is the variation's
own Key type, else its Key-matched backbone post's, else the operator-assigned one; a
variation nothing describes is reported uncovered, never priced under a default colour.

Output is one row per valid combination (the 6-tuple) for
POST /admin/valid-combinations/import -- no nested blob, no size ceiling:

    product_category_id, type_id, variation_id, finish_id, color_id, quality_id

The variation id comes from the live export, so products must be regenerated against
that export BEFORE this runs (see RUNBOOK). A variation whose type cannot be resolved
at all is reported in review/tree_uncovered_variations.csv.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import CATEGORIES, SETTINGS, default_form_name
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.text import looks_like_artifact, match_key
from stone_pipeline.reference.loaders import attributes_status_required, variants_status_required
from stone_pipeline.reference.medusa_files import read_attributes, read_variants_export

log = logfmt.get_logger("tree")

# category name -> plural group key, derived from the pack-built category registry (was hardcoded).
_PREFIX_CATEGORY = {c.name: c.plural for c in CATEGORIES}
COMBINATION_COLUMNS = ("product_category_id", "type_id", "variation_id",
                       "finish_id", "color_id", "quality_id")


def _load_attributes(path: Path) -> dict[str, dict[str, str]]:
    """attributes.csv (category,value,sourceid) -> {kind: {match_key(value): sourceId}}, for every value that
    has an id. Keyed by match_key so a lookup matches regardless of separator/case/accent (a hyphenated
    'Semi-Precious Stone' resolves from an underscore/space slug); lookups must key the same way. The
    read_attributes guards conflicting duplicates."""
    attr: dict[str, dict[str, str]] = defaultdict(dict)
    for a in read_attributes(Path(path), require_status=attributes_status_required(Path(path))):
        if a.sourceid:
            attr[match_key(a.category)][match_key(a.value)] = a.sourceid
    return attr


def _read_backbone(paths: list[Path]):
    for p in paths:
        if not Path(p).exists():
            continue
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        for post in (data["posts"] if isinstance(data, dict) else data):
            yield post


def _load_backbone(paths: list[Path]) -> tuple[dict[str, dict], dict, dict]:
    """Three lookups: by Key (primary join), by (category, type, name) and by (type, name), to recover
    variations whose Key isn't in the backbone (the block backbone is ~99% keyless, tiles mirror slabs). A
    variety's identity is (type, name): a same-name post, of the same category or another, lends its colours
    ONLY when its type is the variation's own, so a same-name post of a different stone never does.

    KNOWN, ACCEPTED STATE (not a bug, do not "clean up" blindly): the block backbone is ~97% KEYLESS
    (~327/12160 keyed) while slab and tile are fully keyed. That is why the by-name lookups above exist and
    why build_combinations recovers a block variation's colour/quality three other ways -- the name lookups
    here, the same-(type,name) slab cross-form inheritance, and the operator leaf-decision overlay
    (backbone_leaf_overlay). Net effect: the pipeline produces CORRECT output despite the keyless block
    backbone; it is compensated in code, not left broken. A full re-key of backbone_blocks.json (to make
    block join by Key like slab/tile) is deliberately NOT done as a file edit: block's Keys are what
    Medusa's EXISTING block products are matched on, so re-keying orphans those products unless Medusa
    re-imports them -- i.e. it is a coordinated migration (re-key + re-import + dev-verified), not a
    correction. Until that migration happens, the compensation above is the intended handling. Verified
    live (dev + prod) after PRs #438 (slab->block colour inheritance) and #440 (honour the leaf overlay)."""
    by_key: dict[str, dict] = {}
    by_cat_name: dict[tuple, dict] = {}
    by_type_name: dict[tuple, dict] = {}
    for post in _read_backbone(paths):
        if post.get("key"):
            by_key[post["key"]] = post
        name = match_key(post.get("variant") or "")   # canonical name key (accent/separator/punct-proof)
        cat = (post.get("category") or "").strip().lower()
        typ = match_key(post.get("stone_type") or "")
        if name:
            by_cat_name.setdefault((cat, typ, name), post)
            by_type_name.setdefault((typ, name), post)
    return by_key, by_cat_name, by_type_name


def _category_finishes(paths: list[Path], attr: dict, products: dict) -> dict[str, list[str]]:
    """Key-prefix -> EVERY finish the category supports (all distinct finishes in its
    backbone, plus any its products use). Tiles mirror slabs."""
    # EVERY finish the category supports (all distinct finishes in its backbone, plus
    # any its products use) -- so each variation is priceable in any finish and a future
    # product is far more likely to already match, no rebuild needed. Tiles mirror slabs.
    cat_to_prefix = {v: k for k, v in _PREFIX_CATEGORY.items()}
    out: dict[str, set] = defaultdict(set)
    for post in _read_backbone(paths):
        prefix = cat_to_prefix.get((post.get("category") or "").strip().lower())
        if prefix:
            for f in post.get("finishes") or []:
                fid = attr["finish"].get(match_key(f))
                if fid:
                    out[prefix].add(fid)
    pcat_to_prefix = {attr["category"].get(_PREFIX_CATEGORY[p]): p for p in _PREFIX_CATEGORY}
    for p in products.values():
        prefix = pcat_to_prefix.get(p["category"])
        if prefix:
            out[prefix] |= p["finishes"]
    # a mirror category inherits the finishes of the category it mirrors (stone: tile mirrors slab), per the
    # pack's declared mirror_of -- so this is not a slab/tile hardcode.
    for c in CATEGORIES:
        if c.mirror_of:
            out[c.name] |= out[c.mirror_of]
    return {k: sorted(v) for k, v in out.items()}


def _load_products(path: Path | None) -> dict[str, dict]:
    """Variation sourceId -> the combo the scrape gave it: {category, colors, quals,
    finishes}. A variation may carry several products (several colours). The type is not
    read: a variation's type is its own Key's (see build_combinations)."""
    out: dict[str, dict] = {}
    if not path or not Path(path).exists():
        return out
    with Path(path).open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            vid = (r.get("STN Variation Id") or "").strip()
            if not vid:
                continue
            d = out.setdefault(vid, {"category": "", "colors": set(), "quals": set(), "finishes": set()})
            d["category"] = (r.get("Product Category 1") or "").strip() or d["category"]
            for key, col in (("colors", "STN Color Id"), ("quals", "STN Quality Id"),
                             ("finishes", "STN Finish Id")):
                v = (r.get(col) or "").strip()
                if v:
                    d[key].add(v)
    return out


# The emitted products sheet's id columns, in COMBINATION_COLUMNS order (product_category_id, type_id,
# variation_id, finish_id, color_id, quality_id), so a product tuple intersects the fan-out tuples directly.
_PRODUCT_TUPLE_COLS = ("Product Category 1", "STN Type Id", "STN Variation Id",
                       "STN Finish Id", "STN Color Id", "STN Quality Id")


def _load_product_tuples(path: Path | None) -> set[tuple]:
    """The EXACT (category, type, variation, finish, colour, quality) id tuple of every emitted product, from
    the same products sheet build_combinations already reads (VE2 PR5). Used to KEEP an emitted product's own
    combination when a value is restricted: the fan-out is filtered to assignable values, then these exact
    tuples are re-added ONLY where the fan-out already produced them (an intersection), so the result stays a
    subset of today's set. A row missing any id is skipped (it is not a priceable combination)."""
    out: set[tuple] = set()
    if not path or not Path(path).exists():
        return out
    with Path(path).open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            t = tuple((r.get(col) or "").strip() for col in _PRODUCT_TUPLE_COLS)
            if all(t):
                out.add(t)
    return out


def _load_assigned_types(path: Path, attr: dict) -> dict[str, str]:
    """review/tree_uncovered_variations.csv 'assign_type' -> {variant_name_lower: type_id}: a stone
    type the user gives a variation the scrape never typed, so it gets allocated to combinations on
    the next run (closes the 'found a variant but it has no type' gap)."""
    out: dict[str, str] = {}
    if Path(path).exists():
        with Path(path).open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                raw = (r.get("assign_type") or "").strip()
                nm = match_key(r.get("variant") or "")
                if not (raw and nm):
                    continue
                tid = attr["type"].get(match_key(raw))
                if tid:
                    out[nm] = tid
                else:                                   # typo'd / unknown type -> don't fail silently
                    log.warning("assign_type is not a known stone type; variation stays uncovered",
                                extra={"extra_fields": {"variant": r.get("variant"), "assign_type": raw}})
    return out


def _write_uncovered(uncovered: list[dict], path: Path) -> None:
    """Surface variations that got NO combinations (no resolvable stone type) so they are NEVER
    silently dropped: one row per variety with an 'assign_type' column the user fills. A row that
    ALREADY carries an assign_type is preserved even once it's covered -- otherwise the variant would
    drop off the file and fall back to uncovered next run (the assignment must persist)."""
    prior: dict[str, dict] = {}
    if Path(path).exists():
        with Path(path).open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                nm = match_key(r.get("variant") or "")
                if nm:
                    prior[nm] = {"assign_type": (r.get("assign_type") or "").strip(),
                                 "variant": (r.get("variant") or "").strip(), "key": (r.get("key") or "").strip(),
                                 "missing": (r.get("missing") or "").strip()}
    rows: list[dict] = []
    seen: set = set()
    for nm, r in prior.items():                          # keep every persisted assignment (type sticks)
        if r["assign_type"]:
            rows.append(r)
            seen.add(nm)
    for u in uncovered:                                  # add still-uncovered varieties (blank -> needs you)
        nm = match_key(u["Name"])
        if nm in seen:
            continue
        seen.add(nm)
        rows.append({"assign_type": prior.get(nm, {}).get("assign_type", ""),
                     "variant": u["Name"], "key": u["Key"], "missing": u.get("missing", "")})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=["assign_type", "variant", "key", "missing"])
        w.writeheader()
        w.writerows(rows)


def _key_type(key: str, attr: dict) -> str | None:
    """The type id carried by the Key itself (branch_<type>_<name>_<uuid>, longest slug match), the same
    authority reconcile pins the product's type to. None when the Key carries no known type: a type is then
    the Key-matched backbone post's or the operator's, never a word guessed out of the Name."""
    parts = key.split("_")[1:-1]  # drop branch prefix and trailing uuid
    for i in range(len(parts), 0, -1):
        tid = attr["type"].get(match_key(" ".join(parts[:i])))
        if tid:
            return tid
    return None


def build_combinations(export_csv: Path, attributes_csv: Path, backbone_paths: list[Path],
                       products_csv: Path | None,
                       assigned_types: dict[str, str] | None = None,
                       exclude_ids: set[str] | None = None,
                       retired_keys: set[str] | None = None,
                       leaf_overlay: "dict[tuple[str, str], dict[str, list[str]]] | None" = None,
                       ) -> tuple[set, dict, list[dict]]:
    """Build the set of valid combinations (6-tuples). Returns (combinations, stats,
    uncovered). exclude_ids = variation IDs slated for deletion in Medusa (variants_to_delete);
    retired_keys = variation KEYS the operator retired. Both get NO combinations, but they live in
    DISTINCT namespaces (the export Id churns, the Key is stable) -- so a retired Key must be tested
    against the Key column, never folded into the Id set (where it would never match and be inert).
    leaf_overlay = decisions_store.backbone_leaf_overlay() -- the operator's APPROVED colour/quality
    decisions, keyed (variety_norm, stone_type_norm). tree_build reads the backbone FILES, not the
    in-memory Backbone that reference_data grows with this overlay, so without it a variety whose colour
    the operator already decided (but which is minted-and-drifted out of the backbone files) is re-flagged
    UNCOVERED here even though the decision exists. Applying it makes the combinations honour the same
    decisions the matching pipeline already does (additive, like Backbone.apply_leaf_overlay)."""
    attr = _load_attributes(attributes_csv)
    # universal finish fallback: a variation in a category that declares NO finish is still priceable in the
    # pack's raw/unfinished finish (stone: 'Raw') rather than dropping uncovered. Sourced from the active
    # pack, not the literal "raw", so a non-stone pack whose unfinished finish is named differently works.
    _raw_finish = attr["finish"].get(match_key(active_pack().block_finish))
    assigned_types = assigned_types or {}
    exclude_ids = exclude_ids or set()
    retired_keys = retired_keys or set()
    by_key, by_cat_name, by_type_name = _load_backbone(backbone_paths)
    products = _load_products(products_csv)
    # VE2 PR5: the restricted (archived/internal) attribute ids and the exact emitted-product tuples. Both empty
    # in the expand phase (no status column), so the fan-out below is byte-identical until then.
    restricted_ids = {a.sourceid for a in read_attributes(
        Path(attributes_csv), require_status=attributes_status_required(Path(attributes_csv)))
        if not a.assignable and a.sourceid}
    emitted_tuples = _load_product_tuples(products_csv)
    archived_keys: set[str] = set()
    # Operator leaf decisions as attribute IDs, keyed (variety_norm, stone_type_norm) -- precomputed once so
    # the per-row lookup below is a dict hit, not a re-map of every decision each of ~36k rows.
    overlay_ids: dict[tuple[str, str], tuple[set, set]] = {}
    for (vnorm, tnorm), adds in (leaf_overlay or {}).items():
        cids = {attr["color"][mk] for c in adds.get("color", []) if (mk := match_key(c)) in attr["color"]}
        qids = {attr["quality"][mk] for q in adds.get("quality", []) if (mk := match_key(q)) in attr["quality"]}
        if cids or qids:
            overlay_ids[(vnorm, tnorm)] = (cids, qids)
    cat_finishes = _category_finishes(backbone_paths, attr, products)
    cat_pcat = {p: attr["category"].get(match_key(c)) for p, c in _PREFIX_CATEGORY.items()}
    type_key = {tid: k for k, tid in attr["type"].items()}              # type id -> the post's type key

    def combo(post) -> tuple:  # (type, colours, quals) from a backbone post, as ids
        return (attr["type"].get(match_key(post.get("stone_type") or "")),
                {attr["color"].get(match_key(x)) for x in (post.get("color") or [])},
                {attr["quality"].get(match_key(x)) for x in (post.get("qualities") or [])})

    export_rows = []
    malformed = 0
    for r in read_variants_export(Path(export_csv), require_status=variants_status_required(Path(export_csv))):
        # a row missing Id or Key is skipped, not a KeyError (the cold-start guard only checks the export
        # EXISTS; read_variants_export fails loud on a missing Key or Name column, an absent Id reads empty).
        vid, key, name = r.id, r.key, r.name
        if r.archived and key:
            archived_keys.add(key)   # VE2 PR5: a frozen variety is counted frozen, not uncovered (below)
        # exclude code-like names (e.g. a stale 'Z Astoria' still in the export) so no
        # combination references a variation that will not exist after the Medusa cleanup.
        if vid and key and vid not in exclude_ids and key not in retired_keys and not looks_like_artifact(name):
            export_rows.append((key, vid, name))
        elif (vid or key or name) and not (vid and key):
            # a row that carries data but is MISSING Id or Key is not a blank line -- it is a real
            # variation being silently dropped from combinations. Surface it (fail-loud, without
            # aborting the run on a thin/header-only cold-start export, which is legitimately empty).
            malformed += 1
    if malformed:
        log.warning("export rows missing Id/Key were skipped from combinations",
                    extra={"extra_fields": {"count": malformed, "export": str(export_csv)}})

    def variation_type(key: str, nl: str):
        """TYPE authority: the variation's own Key type (longest slug match), else its OWN backbone post's
        type (KEY match only -- a name-only post can be a different stone), else a user-assigned type."""
        kpost = by_key.get(key)
        return _key_type(key, attr) or (combo(kpost)[0] if kpost else None) or assigned_types.get(nl)

    # (type, variety name) -> the colours/qualities its products were sold in, in any category, so fan-out
    # mirrors in other categories inherit them. Keyed by the variety identity (type, name): a product of one
    # stone never colours a same-name variety of another type.
    variety: dict[tuple, dict] = {}
    for key, vid, name in export_rows:
        nl = match_key(name)
        if (p := products.get(vid)) and (typ := variation_type(key, nl)):
            d = variety.setdefault((typ, nl), {"colors": set(), "quals": set()})
            d["colors"] |= p["colors"]
            d["quals"] |= p["quals"]

    # Default-form (slab) colour/quality by (type, variety-name), for cross-form inheritance below. Keyed
    # to each default-form EXPORT row's own resolved post (by KEY), not the post's name: the block backbone
    # is keyless and its post names have drifted from the export, so slab (100% keyed) is the only place a
    # variety's canonical colour/quality is reliably reachable by the clean export name.
    _default_prefix = default_form_name()
    df_cq: dict[tuple, dict] = {}
    for key, vid, name in export_rows:
        if key.split("_", 1)[0] == _default_prefix:
            p = by_key.get(key)
            if p:
                t, c, q = combo(p)
                d = df_cq.setdefault((t, match_key(name)), {"colors": set(), "quals": set()})
                d["colors"] |= c
                d["quals"] |= q

    combinations: set = set()

    def add(cat, typ, vid, finishes, colors, quals) -> bool:
        """Add every finish x colour x quality combination; needs at least one of each
        level or the variation can't be priced."""
        finishes = sorted(f for f in finishes if f)
        colors = sorted(c for c in colors if c)
        quals = sorted(q for q in quals if q)
        if not (cat and typ and vid and finishes and colors and quals):
            return False
        for f in finishes:
            for c in colors:
                for q in quals:
                    combinations.add((cat, typ, vid, f, c, q))
        return True

    covered = 0
    frozen_count = 0
    uncovered: list[dict] = []
    for key, vid, name in export_rows:
        prefix = key.split("_", 1)[0]
        nl = match_key(name)   # canonical: joins by_cat_name/variety/assigned_types keyed the same
        # UNION every source's colours/qualities for the widest valid set (max match): the variety's
        # backbone, the scraped product, and a same-(type, variety) product in another category.
        # Finishes = every finish the category supports.
        # The variation's TYPE is intrinsic to the variation (its Key), NOT what a product asserted. A
        # product mis-bound across a name collision or base<->backbone drift must never inject its own type
        # as a valid combination for THIS variation -- that shipped cross-type products the gate can't
        # catch, because it validates products against combinations the products themselves created. So
        # TYPE is pinned to the Key (the same authority reconcile pins the product's type to); only COLOUR
        # and QUALITY union every source, since a variety legitimately spans several colours/qualities.
        colors, quals = set(), set()
        if prod := products.get(vid):
            colors |= prod["colors"]
            quals |= prod["quals"]
        kpost = by_key.get(key)                                          # the variation's OWN backbone post
        typ = variation_type(key, nl)
        # colour/quality sources, all the SAME variety (type, name): its own post, else the same-category post
        # of its name and type, else a same-name post of another category with its type
        post = kpost
        if post is None and typ:
            tk = type_key.get(typ, "")
            post = by_cat_name.get((prefix + "s", tk, nl)) or by_type_name.get((tk, nl))
        if post:
            _t, c, q = combo(post)
            colors |= c
            quals |= q
        if inh := variety.get((typ, nl)):
            colors |= inh["colors"]
            quals |= inh["quals"]
        # Operator leaf decisions (config.db overlay): a colour/quality the operator has already APPROVED is
        # authoritative membership for this (variety, type), the same overlay reference_data grows the
        # matching backbone with. Applied here so a decided variety is never re-flagged uncovered by the
        # combination build (which reads the backbone FILES, not the overlay-grown Backbone). Additive,
        # matching Backbone.apply_leaf_overlay; type-scoped so a same-name different stone never inherits it.
        if typ and (ov := overlay_ids.get((nl, type_key.get(typ, "")))):
            colors |= ov[0]
            quals |= ov[1]
        # Cross-form inheritance: a non-default-form variation (block/tile) that documents no colour or
        # quality of its OWN inherits it from the SAME (type, variety) default-form (slab) export row -- the
        # canonical variety vocabulary. Type-matched (never a same-name different stone) and fills ONLY an
        # empty dimension, so a form with its own value is never widened (that would change existing
        # combinations). This is why block, whose backbone is keyless and drifted, is priceable from slab.
        if prefix != _default_prefix and (not colors or not quals):
            if df := df_cq.get((typ, nl)):
                if not colors:
                    colors |= df["colors"]
                if not quals:
                    quals |= df["quals"]
        # No colour/quality default: a variation nothing describes is UNCOVERED (the review file asks the
        # operator), never priced under the catalogue's most common colour.
        finishes = cat_finishes.get(prefix, []) or ([_raw_finish] if _raw_finish else [])
        if add(cat_pcat.get(prefix), typ, vid, finishes, colors, quals):
            covered += 1
        elif key in archived_keys:
            # VE2 PR5: a FROZEN (archived) variety with no combination is counted frozen, NOT uncovered -- it
            # is not awaiting an operator decision (it will not be served), so it must not sit in the review file.
            frozen_count += 1
        else:
            missing = [n for n, v in (("type", typ), ("finish", finishes), ("colour", colors), ("quality", quals))
                       if not v]
            uncovered.append({"Key": key, "Id": vid, "Name": name, "category": prefix, "missing": "+".join(missing)})

    # VE2 PR5: fan out only to ASSIGNABLE values. Drop a fan-out tuple that carries a restricted attribute id
    # (category pcat / type / finish / colour / quality -- positions 0,1,3,4,5 of COMBINATION_COLUMNS; position
    # 2 is the variation id, a different namespace, never matched). Then RE-ADD the exact tuples of emitted
    # products, but only where the fan-out ALREADY produced them (an intersection), so the result is always a
    # SUBSET of today's set -- an emitted product carrying a restricted value (validate allows it only when the
    # product is acked with it) keeps its combination, and nothing new is ever added. Dormant: an empty
    # restricted set leaves `combinations` untouched (byte-identical).
    missing_product_tuples = len(emitted_tuples - combinations)
    if restricted_ids:
        _attr_pos = (0, 1, 3, 4, 5)
        assignable = {c for c in combinations if not restricted_ids.intersection(c[p] for p in _attr_pos)}
        combinations = assignable | (combinations & emitted_tuples)
    stats = {"covered": covered, "uncovered": len(uncovered), "frozen": frozen_count,
             "combination_rows": len(combinations),
             "variations": len({c[2] for c in combinations}),
             "categories": len({c[0] for c in combinations}),
             "product_tuples_absent_from_fanout": missing_product_tuples,
             "category_finish_counts": {k: len(v) for k, v in cat_finishes.items()}}
    if missing_product_tuples:
        log.warning("emitted product tuples absent from the fan-out (kept none of these; a separate finding)",
                    extra={"extra_fields": {"count": missing_product_tuples}})
    return combinations, stats, uncovered


def write_combinations(combinations: set, path: Path) -> int:
    """Write the valid-combination rows (sorted for a deterministic file), ATOMICALLY -- a crash
    mid-write must never leave a truncated file for the consistency gate to check or the publish to
    hand Blokport as the whole set. Returns the row count."""
    return csvio.write_rows(path, COMBINATION_COLUMNS, sorted(combinations))


def _backbone_paths() -> list[Path]:
    """Every category backbone + the per-run backbone_additions (new varieties)."""
    paths = [c.backbone_path for c in CATEGORIES]
    additions = SETTINGS.paths.catalog_source_dir / "backbone_additions"
    if additions.exists():
        paths += sorted(additions.glob("*.json"))
    return paths


def _product_variation_ids(products_csv: Path) -> set[str]:
    """Variation ids the product sheet actually uses (for the products-only one-off)."""
    if not products_csv.exists():
        return set()
    with products_csv.open(encoding="utf-8-sig", newline="") as h:
        return {(r.get("STN Variation Id") or "").strip()
                for r in csv.DictReader(h) if (r.get("STN Variation Id") or "").strip()}


def run() -> Path:
    """Build the valid combinations and write the upload set to to_upload/:
      2_valid_combinations.csv          FULL set  -- Blokport replaces its set with it when the manifest's
                                                    sha256 changed (no delta: an import never adds into a set)
      2_valid_combinations_products_only.csv      combos for just the variations the product sheet uses
                                                    (written in every env; Blokport can load it instead)
    """
    export = SETTINGS.paths.export_file
    if not export.exists():
        # Cold start: Medusa has 0 variations yet, so there is no export and nothing to build combinations
        # against until THIS produce's variants are pulled and minted. Write EMPTY sets (not a hard abort)
        # so pass 1 completes and the variants flow to Medusa; the next republish -- against the now-real
        # export -- builds the combinations. WARNING (not silent) so a WARM run with a forgotten export is
        # still visible (it also surfaces downstream as 0 products emitted).
        to_upload = SETTINGS.paths.to_upload_dir
        log.warning("no variants export; writing EMPTY combinations (cold start: nothing to build against "
                    "until Medusa mints this produce's variations)",
                    extra={"extra_fields": {"export": str(export)}})
        for name in ("2_valid_combinations.csv", "2_valid_combinations_products_only.csv"):
            write_combinations(set(), to_upload / name)
        return to_upload / "2_valid_combinations.csv"
    products = SETTINGS.paths.to_upload_dir / "3_products_all.csv"
    uncovered_file = SETTINGS.paths.review_dir / "tree_uncovered_variations.csv"
    assigned = _load_assigned_types(uncovered_file, _load_attributes(SETTINGS.paths.attributes_csv))
    delete_file = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    exclude_ids: set[str] = set()
    if delete_file.exists():
        with delete_file.open(encoding="utf-8-sig") as h:   # close the handle (was leaked in a comprehension)
            exclude_ids = {(r.get("Id") or "").strip() for r in csv.DictReader(h) if (r.get("Id") or "").strip()}
    # E10: retired varieties (the operator's explicit removal / un-retire memory) also get no combinations,
    # so a retired Key is never re-priced/re-served. load_retired() returns KEYS, which are a DISTINCT
    # namespace from the export Ids in exclude_ids -- pass them separately so they are tested against the
    # Key column, not silently merged into the Id set (where they would never match and leak combinations).
    from stone_pipeline.stages import decisions
    from stone_pipeline.config import decisions_store
    retired_keys = decisions.load_retired()
    combinations, stats, uncovered = build_combinations(
        export, SETTINGS.paths.attributes_csv, _backbone_paths(),
        products if products.exists() else None, assigned, exclude_ids, retired_keys,
        leaf_overlay=decisions_store.backbone_leaf_overlay())

    to_upload = SETTINGS.paths.to_upload_dir
    path = to_upload / "2_valid_combinations.csv"
    write_combinations(combinations, path)

    # combinations for ONLY the product sheet's variations (written in every env, beside the full set)
    vidx = COMBINATION_COLUMNS.index("variation_id")
    prod_vids = _product_variation_ids(products)
    products_only = {c for c in combinations if c[vidx] in prod_vids} if prod_vids else set()
    write_combinations(products_only, to_upload / "2_valid_combinations_products_only.csv")

    # SURFACE the variations that got no combination (no resolvable type) so they are never silently
    # dropped -- the user fills 'assign_type' and they get allocated next run.
    _write_uncovered(uncovered, uncovered_file)
    stats.update(uncovered_variations=len(uncovered), combinations_total=len(combinations),
                 combinations_products_only=len(products_only))
    log.info("valid combinations built", extra={"extra_fields": stats})
    return path
