"""Build the valid-combination set for Medusa's relational `valid_combination` table.

FULL combination set (the relational table has no size ceiling, so we pre-load every
possible combination — a future product is then far more likely to already match,
needing no rebuild). Each variation gets EVERY finish its category supports x the UNION
of every colour/quality we know for the variety (its backbone post, the scraped product,
and a same-variety product in another category). The product's type wins so products
always match; a no-data variation falls back to the type parsed from its Key/Name + the
catalogue's most common colour.

Output is one row per valid combination (the 6-tuple) for
POST /admin/valid-combinations/import — no nested blob, no size ceiling:

    product_category_id, type_id, variation_id, finish_id, color_id, quality_id

The variation id comes from the live export, so products must be regenerated against
that export BEFORE this runs (see RUNBOOK). A variation whose type cannot be resolved
at all is reported in review/tree_uncovered_variations.csv.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from stone_pipeline.config.settings import CATEGORIES, SETTINGS
from stone_pipeline.core import logfmt
from stone_pipeline.core.text import looks_like_artifact

log = logfmt.get_logger("tree")

_PREFIX_CATEGORY = {"slab": "slabs", "block": "blocks", "tile": "tiles"}
COMBINATION_COLUMNS = ("product_category_id", "type_id", "variation_id",
                       "finish_id", "color_id", "quality_id")


def _load_attributes(path: Path) -> dict[str, dict[str, str]]:
    """attributes.csv (category,value,sourceid) -> {kind: {value_lower: sourceId}}."""
    attr: dict[str, dict[str, str]] = defaultdict(dict)
    with Path(path).open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            attr[r["category"].strip().lower()][r["value"].strip().lower()] = r["sourceid"].strip()
    return attr


def _read_backbone(paths: list[Path]):
    for p in paths:
        if not Path(p).exists():
            continue
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        for post in (data["posts"] if isinstance(data, dict) else data):
            yield post


def _load_backbone(paths: list[Path]) -> tuple[dict[str, dict], dict, dict]:
    """Three lookups: by Key (primary join) + two NAME indexes to recover variations
    whose Key isn't in the backbone (the block backbone is ~99% keyless, tiles mirror
    slabs). by_cat_name keeps category-correct combos."""
    by_key: dict[str, dict] = {}
    by_cat_name: dict[tuple, dict] = {}
    by_name: dict[str, dict] = {}
    for post in _read_backbone(paths):
        if post.get("key"):
            by_key[post["key"]] = post
        name = (post.get("variant") or "").strip().lower()
        cat = (post.get("category") or "").strip().lower()
        if name:
            by_cat_name.setdefault((cat, name), post)
            by_name.setdefault(name, post)
    return by_key, by_cat_name, by_name


def _category_finishes(paths: list[Path], attr: dict, products: dict) -> dict[str, list[str]]:
    """Key-prefix -> EVERY finish the category supports (all distinct finishes in its
    backbone, plus any its products use). Tiles mirror slabs."""
    # EVERY finish the category supports (all distinct finishes in its backbone, plus
    # any its products use) — so each variation is priceable in any finish and a future
    # product is far more likely to already match, no rebuild needed. Tiles mirror slabs.
    cat_to_prefix = {v: k for k, v in _PREFIX_CATEGORY.items()}
    out: dict[str, set] = defaultdict(set)
    for post in _read_backbone(paths):
        prefix = cat_to_prefix.get((post.get("category") or "").strip().lower())
        if prefix:
            for f in post.get("finishes") or []:
                fid = attr["finish"].get((f or "").strip().lower())
                if fid:
                    out[prefix].add(fid)
    pcat_to_prefix = {attr["category"].get(_PREFIX_CATEGORY[p]): p for p in _PREFIX_CATEGORY}
    for p in products.values():
        prefix = pcat_to_prefix.get(p["category"])
        if prefix:
            out[prefix] |= p["finishes"]
    out["tile"] |= out["slab"]
    return {k: sorted(v) for k, v in out.items()}


def _load_products(path: Path | None) -> dict[str, dict]:
    """Variation sourceId -> the combo the scrape gave it: {category, type, colors,
    quals, finishes}. A variation may carry several products (several colours)."""
    out: dict[str, dict] = {}
    if not path or not Path(path).exists():
        return out
    with Path(path).open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            vid = (r.get("STN Variation Id") or "").strip()
            if not vid:
                continue
            d = out.setdefault(vid, {"category": "", "type": "", "colors": set(),
                                     "quals": set(), "finishes": set()})
            d["category"] = (r.get("Product Category 1") or "").strip() or d["category"]
            d["type"] = (r.get("STN Type Id") or "").strip() or d["type"]
            for key, col in (("colors", "STN Color Id"), ("quals", "STN Quality Id"),
                             ("finishes", "STN Finish Id")):
                v = (r.get(col) or "").strip()
                if v:
                    d[key].add(v)
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
                nm = (r.get("variant") or "").strip().lower()
                if not (raw and nm):
                    continue
                tid = attr["type"].get(raw.lower())
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
                nm = (r.get("variant") or "").strip().lower()
                if nm:
                    prior[nm] = {"assign_type": (r.get("assign_type") or "").strip(),
                                 "variant": (r.get("variant") or "").strip(), "key": (r.get("key") or "").strip()}
    rows: list[dict] = []
    seen: set = set()
    for nm, r in prior.items():                          # keep every persisted assignment (type sticks)
        if r["assign_type"]:
            rows.append(r)
            seen.add(nm)
    for u in uncovered:                                  # add still-uncovered varieties (blank -> needs you)
        nm = u["Name"].strip().lower()
        if nm in seen:
            continue
        seen.add(nm)
        rows.append({"assign_type": prior.get(nm, {}).get("assign_type", ""),
                     "variant": u["Name"], "key": u["Key"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=["assign_type", "variant", "key"])
        w.writeheader()
        w.writerows(rows)


def _resolve_type(key: str, name: str, attr: dict) -> str | None:
    """Find the stone type for an un-backboned variation: longest match of the Key's
    type slug (branch_<type>_<name>_<uuid>), else a type word in the Name (e.g.
    'Everest Quartzite' -> Quartzite)."""
    parts = key.split("_")[1:-1]  # drop branch prefix and trailing uuid
    for i in range(len(parts), 0, -1):
        tid = attr["type"].get(" ".join(parts[:i]).lower())
        if tid:
            return tid
    words = name.lower().replace("–", " ").replace("-", " ").split()
    for n in (2, 1):  # a 2- or 1-word type name appearing anywhere in the Name
        for j in range(len(words) - n + 1):
            tid = attr["type"].get(" ".join(words[j:j + n]))
            if tid:
                return tid
    return None


def build_combinations(export_csv: Path, attributes_csv: Path, backbone_paths: list[Path],
                       products_csv: Path | None,
                       assigned_types: dict[str, str] | None = None) -> tuple[set, dict, list[dict]]:
    """Build the set of valid combinations (6-tuples). Returns (combinations, stats,
    uncovered)."""
    attr = _load_attributes(attributes_csv)
    assigned_types = assigned_types or {}
    by_key, by_cat_name, by_name = _load_backbone(backbone_paths)
    products = _load_products(products_csv)
    cat_finishes = _category_finishes(backbone_paths, attr, products)
    cat_pcat = {p: attr["category"].get(c) for p, c in _PREFIX_CATEGORY.items()}

    def combo(post) -> tuple:  # (type, colours, quals) from a backbone post, as ids
        return (attr["type"].get((post.get("stone_type") or "").strip().lower()),
                {attr["color"].get((x or "").strip().lower()) for x in (post.get("color") or [])},
                {attr["quality"].get((x or "").strip().lower()) for x in (post.get("qualities") or [])})

    # export rows + variety identity (Name) -> the colour/type a scrape gave it anywhere,
    # so fan-out mirrors in other categories inherit it, plus catalogue defaults.
    export_rows, name_of = [], {}
    with Path(export_csv).open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            vid = r["Id"].strip()
            name = (r.get("Name") or "").strip()
            # exclude code-like names (e.g. a stale 'Z Astoria' still in the export) so no
            # combination references a variation that will not exist after the Medusa cleanup.
            if vid and not looks_like_artifact(name):
                export_rows.append((r["Key"].strip(), vid, name))
                name_of[vid] = name.lower()
    variety: dict[str, dict] = {}
    color_freq, qual_freq = Counter(), Counter()
    for vid, p in products.items():
        color_freq.update(p["colors"])
        qual_freq.update(p["quals"])
        nm = name_of.get(vid)
        if nm:
            d = variety.setdefault(nm, {"type": "", "colors": set(), "quals": set()})
            d["type"] = p["type"] or d["type"]
            d["colors"] |= p["colors"]
            d["quals"] |= p["quals"]
    default_color = color_freq.most_common(1)[0][0] if color_freq else None
    default_qual = qual_freq.most_common(1)[0][0] if qual_freq else None

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
    uncovered: list[dict] = []
    for key, vid, name in export_rows:
        prefix = key.split("_", 1)[0]
        nl = name.lower()
        # UNION every source's colours/qualities for the widest valid set (max match):
        # the variety's backbone, the scraped product, and a same-variety product in
        # another category. Finishes = every finish the category supports. The PRODUCT's
        # type wins (it's what the product row carries, so the product always matches).
        typ, colors, quals = None, set(), set()
        if prod := products.get(vid):
            typ, colors, quals = prod["type"], colors | prod["colors"], quals | prod["quals"]
        kpost = by_key.get(key)                                          # the variation's OWN backbone post
        post = kpost or by_cat_name.get((prefix + "s", nl)) or by_name.get(nl)
        if post:
            t, c, q = combo(post)
            colors |= c
            quals |= q
            # adopt the post's TYPE only from a KEY match. A name-only post (by_cat_name/by_name) can
            # be a DIFFERENT stone of the same name -> using its type mis-prices the variation
            # ('Pietra' the Marble priced as Granite). Its colour/quality are still safe to union.
            if kpost:
                typ = typ or t
        if inh := variety.get(nl):
            colors |= inh["colors"]                                     # colour/quality inherit by name OK
            quals |= inh["quals"]                                       # but NOT type (same cross-type risk)
        typ = typ or _resolve_type(key, name, attr) or assigned_types.get(nl)   # the variation's own Key, else user-assigned
        colors = colors or {default_color}                 # last resort: catalogue defaults
        quals = quals or {default_qual}
        if add(cat_pcat.get(prefix), typ, vid, cat_finishes.get(prefix, []), colors, quals):
            covered += 1
        else:
            uncovered.append({"Key": key, "Id": vid, "Name": name, "category": prefix})

    stats = {"covered": covered, "uncovered": len(uncovered),
             "combination_rows": len(combinations),
             "variations": len({c[2] for c in combinations}),
             "categories": len({c[0] for c in combinations}),
             "category_finish_counts": {k: len(v) for k, v in cat_finishes.items()}}
    return combinations, stats, uncovered


def write_combinations(combinations: set, path: Path) -> int:
    """Write the valid-combination rows (sorted for a deterministic file). Returns the
    row count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(COMBINATION_COLUMNS)
        w.writerows(sorted(combinations))
    return len(combinations)


def _backbone_paths() -> list[Path]:
    """Every category backbone + the per-run backbone_additions (new varieties)."""
    paths = [c.backbone_path for c in CATEGORIES]
    additions = SETTINGS.paths.catalog_source_dir / "backbone_additions"
    if additions.exists():
        paths += sorted(additions.glob("*.json"))
    return paths


def _load_combination_set(path: Path) -> set[tuple[str, ...]]:
    """Read a combinations CSV back into a set of string-tuples (to diff builds). Empty if absent."""
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as h:
        rows = csv.reader(h)
        next(rows, None)  # skip header
        return {tuple(row) for row in rows if row}


def _product_variation_ids(products_csv: Path) -> set[str]:
    """Variation ids the product sheet actually uses (for the products-only one-off)."""
    if not products_csv.exists():
        return set()
    with products_csv.open(encoding="utf-8-sig", newline="") as h:
        return {(r.get("STN Variation Id") or "").strip()
                for r in csv.DictReader(h) if (r.get("STN Variation Id") or "").strip()}


def run() -> Path:
    """Build the valid combinations and write the upload set to to_upload/:
      2_valid_combinations.csv          FULL set  -- one-time bulk load / resync (huge)
      2_valid_combinations_update.csv   DELTA     -- only combinations NEW since the last build; this
                                                    is the DAILY upload (add into the existing set)
      2_valid_combinations_products_only.csv      TESTING one-off (drops off in production): combos
                                                    for just the variations the product sheet uses.
    """
    export = SETTINGS.paths.export_file
    if not export.exists():
        raise SystemExit(f"no variants export: {export} (download it from Medusa first)")
    products = SETTINGS.paths.to_upload_dir / "3_products_all.csv"
    uncovered_file = SETTINGS.paths.review_dir / "tree_uncovered_variations.csv"
    assigned = _load_assigned_types(uncovered_file, _load_attributes(SETTINGS.paths.attributes_csv))
    combinations, stats, uncovered = build_combinations(
        export, SETTINGS.paths.attributes_csv, _backbone_paths(),
        products if products.exists() else None, assigned)

    to_upload = SETTINGS.paths.to_upload_dir
    path = to_upload / "2_valid_combinations.csv"
    # The PREVIOUS full file is the baseline. Normalise to string-tuples so the in-memory build diffs
    # cleanly against the CSV-read baseline.
    current = {tuple(str(x) for x in c) for c in combinations}
    previous = _load_combination_set(path)
    write_combinations(current, path)                                   # FULL: bulk load / resync

    # INCREMENTAL delta -- only the combinations NEW vs the last build, so the daily upload adds into
    # the existing Medusa set instead of re-sending ~2M rows. First build (no baseline) -> delta is
    # the full set. If a daily delta is ever skipped, re-upload the FULL file once to resync.
    additions = current - previous
    removed = previous - current
    write_combinations(additions, to_upload / "2_valid_combinations_update.csv")

    # TESTING one-off (drops off in production): combinations for ONLY the product sheet's variations.
    vidx = COMBINATION_COLUMNS.index("variation_id")
    prod_vids = _product_variation_ids(products)
    products_only = {c for c in current if c[vidx] in prod_vids} if prod_vids else set()
    write_combinations(products_only, to_upload / "2_valid_combinations_products_only.csv")

    # SURFACE the variations that got no combination (no resolvable type) so they are never silently
    # dropped -- the user fills 'assign_type' and they get allocated next run.
    _write_uncovered(uncovered, uncovered_file)
    stats.update(uncovered_variations=len(uncovered), combinations_total=len(current),
                 combinations_added=len(additions), combinations_removed=len(removed),
                 combinations_products_only=len(products_only))
    log.info("valid combinations built", extra={"extra_fields": stats})
    return path
