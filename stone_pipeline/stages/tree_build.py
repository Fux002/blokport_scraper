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
from collections import defaultdict
from pathlib import Path

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.categories import CATEGORIES, category_by_handle, category_by_pcat, default_form_name
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import csvio, logfmt
from stone_pipeline.core.text import looks_like_artifact, match_key
from stone_pipeline.reference.loaders import attributes_status_required, type_slug_from_key, variants_status_required
from stone_pipeline.reference.loaders.backbone import read_posts
from stone_pipeline.reference.medusa_files import CATEGORY_KIND, read_attributes, read_variants_export

log = logfmt.get_logger("tree")

# The categories a variety is priced in: every one a new variety fans out to (phase 1: a variety is ONE record per
# (type, name) and has no category of its own, so it is offered in each). Category NAME -> its handle.
_FAN_OUT_CATEGORIES = {c.name: c.handle for c in CATEGORIES if c.fan_out}
COMBINATION_COLUMNS = ("product_category_id", "type_id", "variation_id",
                       "finish_id", "color_id", "quality_id")


def _load_attributes(path: Path) -> dict[str, dict[str, str]]:
    """attributes.csv (category,value,sourceid) -> {kind: {match_key(value): sourceId}}, for every value that
    has an id. Keyed by match_key so a lookup matches regardless of separator/case/accent (a hyphenated
    'Semi-Precious Stone' resolves from an underscore/space slug); lookups must key the same way. The
    read_attributes guards conflicting duplicates. Category rows are skipped: a category's pcat is the registry's,
    by its exact handle (_category_pcat)."""
    attr: dict[str, dict[str, str]] = defaultdict(dict)
    for a in read_attributes(Path(path), require_status=attributes_status_required(Path(path))):
        if a.sourceid and a.category != CATEGORY_KIND:
            attr[match_key(a.category)][match_key(a.value)] = a.sourceid
    return attr


def _category_pcat(handle: str) -> str | None:
    """A category's pcat on this env, by its exact handle (the registry); None when it has none."""
    cat = category_by_handle(handle)
    return (cat.pcat_id or None) if cat is not None else None


def _read_backbone(paths: list[Path]):
    for p in paths:
        if Path(p).exists():
            yield from read_posts(Path(p))


def _load_backbone(paths: list[Path]) -> tuple[dict[str, dict], dict]:
    """Two lookups: by Key (the primary join) and by (type, name), the variety identity (phase 1: one post per
    identity, so a post's colours are its variety's whatever the category)."""
    by_key: dict[str, dict] = {}
    by_type_name: dict[tuple, dict] = {}
    for post in _read_backbone(paths):
        if post.get("key"):
            by_key[post["key"]] = post
        name = match_key(post.get("variant") or "")   # canonical name key (accent/separator/punct-proof)
        if name:
            by_type_name.setdefault((match_key(post.get("stone_type") or ""), name), post)
    return by_key, by_type_name


def _category_finishes(paths: list[Path], attr: dict, products: dict) -> dict[str, list[str]]:
    """Category name -> the finish ids it supports: the default form's are every finish its
    variety posts carry; another category's are its declared `finishes`, or the set of the category named by its
    `finishes_from`. Any finish the category's products use this run is added. A declared finish the vocabulary
    does not hold fails loud (it would otherwise silently price nothing)."""
    out: dict[str, set] = defaultdict(set)
    default = default_form_name()
    for post in _read_backbone(paths):
        for f in post.get("finishes") or []:
            if fid := attr["finish"].get(match_key(f)):
                out[default].add(fid)
    for c in CATEGORIES:
        unresolved = [f for f in c.finishes if match_key(f) not in attr["finish"]]
        if unresolved:
            raise ValueError(f"category {c.name!r} declares finishes the finish vocabulary does not hold: {unresolved}")
        out[c.name] |= {attr["finish"][match_key(f)] for f in c.finishes}
    for p in products.values():
        if (cat := category_by_pcat(p["category"])) is not None:
            out[cat.name] |= p["finishes"]
    for c in CATEGORIES:
        if c.finishes_from:
            out[c.name] |= out[c.finishes_from]
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
    the same products sheet build_combinations already reads. Used to KEEP an emitted product's own
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


def _acked_combination_tuples(attr: dict, nkey_to_vid: dict[str, str], ledger=None) -> tuple[set[tuple], int]:
    """(tuples, unrebuilt) for the ledger-ACKED products (medusa_id set), in COMBINATION_COLUMNS order, so a
    product live in Medusa keeps its combination even when this run emitted nothing for it (held for a
    transient reason, or its source produced nothing). Reconstructed from ledger/existing.acked_products (the
    ONE ledger reader) with names mapped to ids through the attributes vocabulary (`attr`, the single
    authority) and the variation id from the export (keyed by normalized Key). `ledger`, when given, is read
    directly (ledger.verify passes the ledger it was given); else the per-env ledger is opened if it exists.
    `unrebuilt` counts acked products whose tuple could not be fully rebuilt (a missing id/vid), surfaced as a
    finding rather than dropped silently. Empty when there is no ledger."""
    from stone_pipeline.ledger.existing import acked_products
    if ledger is not None:
        acked = acked_products(ledger)
    else:
        from stone_pipeline.ledger import writethrough
        path = writethrough.ledger_path()
        if not path.exists():
            return set(), 0
        with writethrough.connect(path, bind=True) as lg:
            acked = acked_products(lg)
    out: set[tuple] = set()
    unrebuilt = 0
    for carried in acked.values():
        d = dict(carried)   # {dimension: norm(value)}, one value per dimension
        t = (_category_pcat(d.get("category", "")), attr["type"].get(d.get("type", "")),
             nkey_to_vid.get(d.get("variation_key", "")), attr["finish"].get(d.get("finish", "")),
             attr["color"].get(d.get("color", "")), attr["quality"].get(d.get("quality", "")))
        if all(t):
            out.add(t)
        else:
            unrebuilt += 1
    return out, unrebuilt


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
    """The type id the Key itself carries: its type slug (the one parser, phase1.variety_key), the same authority
    reconcile pins the product's type to. None when the slug is no known type: a type is then the Key-matched
    backbone post's or the operator's, never a word guessed out of the Name."""
    return attr["type"].get(match_key(type_slug_from_key(key).replace("_", " ")))


def build_combinations(export_csv: Path, attributes_csv: Path, backbone_paths: list[Path],
                       products_csv: Path | None,
                       assigned_types: dict[str, str] | None = None,
                       exclude_ids: set[str] | None = None,
                       retired_keys: set[str] | None = None,
                       leaf_overlay: "dict[tuple[str, str], dict[str, list[str]]] | None" = None,
                       acked_ledger=None,
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
    by_key, by_type_name = _load_backbone(backbone_paths)
    products = _load_products(products_csv)
    # The restricted (archived/internal) attribute ids and the exact emitted-product tuples. Both empty
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
    cat_pcat = {name: _category_pcat(handle) for name, handle in _FAN_OUT_CATEGORIES.items()}
    if unpriced := sorted(name for name, pcat in cat_pcat.items() if not pcat):
        log.warning("fan-out categories with no category id in attributes.csv get no combinations",
                    extra={"extra_fields": {"categories": unpriced}})
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
            archived_keys.add(key)   # a frozen variety is counted frozen, not uncovered (below)
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
        """TYPE authority: the variation's own Key type slug, else its OWN backbone post's
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

    row_meta: list[tuple] = []        # (key, vid, name), classified AFTER the assignable filter
    premissing: dict[str, str] = {}   # vid -> missing diagnostic when the fan-out produced nothing even pre-filter
    for key, vid, name in export_rows:
        nl = match_key(name)   # canonical: joins by_type_name/variety/assigned_types keyed the same
        # UNION every source's colours/qualities for the widest valid set (max match): the variety's
        # backbone post and its scraped products in every category.
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
        typ = variation_type(key, nl)
        # its own post by Key, else the post of its (type, name): one post per variety (phase 1)
        post = by_key.get(key)
        if post is None and typ:
            post = by_type_name.get((type_key.get(typ, ""), nl))
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
        # No colour/quality default: a variation nothing describes is UNCOVERED (the review file asks the
        # operator), never priced under the catalogue's most common colour.
        row_meta.append((key, vid, name))
        priced = False
        for cat_name, pcat in sorted(cat_pcat.items()):
            finishes = cat_finishes.get(cat_name, []) or ([_raw_finish] if _raw_finish else [])
            priced |= add(pcat, typ, vid, finishes, colors, quals)
        if not priced:
            missing = [n for n, v in (("type", typ), ("colour", colors), ("quality", quals)) if not v]
            premissing[vid] = "+".join(missing)

    # Fan out only to ASSIGNABLE values. Drop a fan-out tuple carrying a restricted attribute id
    # (category pcat / type / finish / colour / quality; the variation-id position is a different namespace,
    # never matched). Then RE-ADD the keep-set INTERSECTED with the fan-out, so the result is always a SUBSET
    # of today's set and nothing new is ever added. The keep-set is the emitted products' exact tuples UNION
    # the ledger-ACKED products' tuples -- a product live in Medusa keeps its combination even when this run
    # emitted nothing for it (held for a transient reason, or its source produced nothing). Dormant: an empty
    # restricted set leaves `combinations` untouched (byte-identical); the ledger is opened only when restricted.
    vidx = COMBINATION_COLUMNS.index("variation_id")
    missing_product_tuples = len(emitted_tuples - combinations)
    unrebuilt_acked = 0
    if restricted_ids:
        acked_tuples, unrebuilt_acked = _acked_combination_tuples(
            attr, {match_key(k): v for k, v, _ in export_rows}, acked_ledger)
        keep_set = set(emitted_tuples) | acked_tuples
        attr_pos = tuple(i for i, col in enumerate(COMBINATION_COLUMNS) if col != "variation_id")
        assignable = {c for c in combinations if not restricted_ids.intersection(c[p] for p in attr_pos)}
        combinations = assignable | (combinations & keep_set)

    # Classify coverage AFTER the filter: a variety left with no combination (its only values were restricted)
    # goes to uncovered -- or frozen, if its variety Key is archived (not awaiting an operator decision).
    covered_vids = {c[vidx] for c in combinations}
    covered = 0
    frozen_count = 0
    uncovered: list[dict] = []
    for key, vid, name in row_meta:
        if vid in covered_vids:
            covered += 1
        elif key in archived_keys:
            frozen_count += 1
        else:
            # dormant: a vid with a pre-filter diagnostic keeps it (even the empty '' main writes when only the
            # category pcat was missing); "restricted" is only for a vid dropped BY the filter (not pre-filter).
            uncovered.append({"Key": key, "Id": vid, "Name": name,
                              "missing": premissing[vid] if vid in premissing else "restricted"})
    stats = {"covered": covered, "uncovered": len(uncovered), "frozen": frozen_count,
             "combination_rows": len(combinations),
             "variations": len({c[vidx] for c in combinations}),
             "categories": len({c[0] for c in combinations}),
             "product_tuples_absent_from_fanout": missing_product_tuples,
             "acked_tuples_unrebuilt": unrebuilt_acked,
             "category_finish_counts": {k: len(v) for k, v in cat_finishes.items()}}
    if missing_product_tuples:
        log.warning("emitted product tuples absent from the fan-out (kept none of these; a separate finding)",
                    extra={"extra_fields": {"count": missing_product_tuples}})
    if unrebuilt_acked:
        log.warning("acked products whose combination tuple could not be rebuilt (a missing id/vid); not kept",
                    extra={"extra_fields": {"count": unrebuilt_acked}})
    return combinations, stats, uncovered


def write_combinations(combinations: set, path: Path) -> int:
    """Write the valid-combination rows (sorted for a deterministic file), ATOMICALLY -- a crash
    mid-write must never leave a truncated file for the consistency gate to check or the publish to
    hand Blokport as the whole set. Returns the row count."""
    return csvio.write_rows(path, COMBINATION_COLUMNS, sorted(combinations))


def _backbone_paths() -> list[Path]:
    """The ONE variety backbone and the ONE additions file (curate's per-run new varieties)."""
    return [SETTINGS.paths.backbone_json, SETTINGS.paths.backbone_additions_file]


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
