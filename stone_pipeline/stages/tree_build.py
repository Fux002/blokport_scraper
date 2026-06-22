"""Build the valid-combination set for Medusa's relational `valid_combination` table.

Every imported variation is made priceable: each gets its category's PRODUCT-USED
finish set (the finishes that category's products actually carry; tiles mirror slabs)
x the colour(s) we know x quality. Colour/type come from the best source available:
the product that sold it, its backbone post, a same-variety product in another
category (fan-out mirrors inherit the scraped colour), or the type parsed from its
Key/Name + the catalogue's most common colour.

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

from stone_pipeline.config import settings
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

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


def _canonical_finishes(paths: list[Path], attr: dict) -> dict[str, set]:
    """Key-prefix -> finish ids present in >=50% of that category's backbone posts
    (the standard finish set; a fallback for categories with no products)."""
    cat_to_prefix = {v: k for k, v in _PREFIX_CATEGORY.items()}
    cnt: dict[str, Counter] = defaultdict(Counter)
    n: dict[str, int] = defaultdict(int)
    for post in _read_backbone(paths):
        prefix = cat_to_prefix.get((post.get("category") or "").strip().lower())
        if not prefix:
            continue
        n[prefix] += 1
        for f in set(post.get("finishes") or []):
            cnt[prefix][f] += 1
    out: dict[str, set] = defaultdict(set)
    for prefix, counts in cnt.items():
        for f, k in counts.items():
            fid = attr["finish"].get(f.strip().lower())
            if fid and k >= n[prefix] * 0.5:
                out[prefix].add(fid)
    return out


def _category_finishes(paths: list[Path], attr: dict, products: dict) -> dict[str, list[str]]:
    """Key-prefix -> the finish ids that category's PRODUCTS actually use. Tiles mirror
    slabs; a category with no products falls back to its canonical backbone finishes."""
    pcat_to_prefix = {attr["category"].get(_PREFIX_CATEGORY[p]): p for p in _PREFIX_CATEGORY}
    used: dict[str, set] = defaultdict(set)
    for p in products.values():
        prefix = pcat_to_prefix.get(p["category"])
        if prefix:
            used[prefix] |= p["finishes"]
    canon = _canonical_finishes(paths, attr)
    out = {prefix: set(used[prefix] if used.get(prefix) else canon.get(prefix, set()))
           for prefix in _PREFIX_CATEGORY}
    out["tile"] |= out["slab"]     # tiles are sold in the same finishes as slabs (mirror)
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
                       products_csv: Path | None) -> tuple[set, dict, list[dict]]:
    """Build the set of valid combinations (6-tuples). Returns (combinations, stats,
    uncovered)."""
    attr = _load_attributes(attributes_csv)
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
            if vid:
                export_rows.append((r["Key"].strip(), vid, (r.get("Name") or "").strip()))
                name_of[vid] = (r.get("Name") or "").strip().lower()
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

    counts: Counter = Counter()
    uncovered: list[dict] = []
    for key, vid, name in export_rows:
        prefix = key.split("_", 1)[0]
        nl = name.lower()
        prod = products.get(vid)
        same = by_key.get(key) or by_cat_name.get((prefix + "s", nl))
        if prod:                                  # sold here: scraped colour(s) + type
            typ, colors, quals, src = prod["type"], prod["colors"], prod["quals"], "product"
        elif same:                                # in the backbone: its colours/quals
            typ, colors, quals = combo(same)
            src = "backbone"
        elif nl in variety:                       # variety sold in another category: inherit
            v = variety[nl]
            typ, colors, quals, src = v["type"], v["colors"], v["quals"], "inherited"
        elif by_name.get(nl):                     # only a cross-category backbone post
            typ, colors, quals = combo(by_name[nl])
            src = "name"
        else:                                     # no data anywhere: type from Key/Name + defaults
            typ = _resolve_type(key, name, attr)
            colors, quals, src = {default_color}, {default_qual}, "default"
        if add(cat_pcat.get(prefix), typ, vid, cat_finishes.get(prefix, []), colors, quals):
            counts[src] += 1
        else:
            uncovered.append({"Key": key, "Id": vid, "Name": name, "category": prefix})

    stats = {"covered": sum(counts.values()), "by_source": dict(counts),
             "uncovered": len(uncovered), "combination_rows": len(combinations),
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
    paths = [c.backbone_path for c in settings.CATEGORIES]
    additions = SETTINGS.paths.catalog_source_dir / "backbone_additions"
    if additions.exists():
        paths += sorted(additions.glob("*.json"))
    return paths


def run() -> Path:
    """Build the valid combinations and write to_upload/2_valid_combinations.csv."""
    export = SETTINGS.paths.export_file
    if not export.exists():
        raise SystemExit(f"no variants export: {export} (download it from Medusa first)")
    products = SETTINGS.paths.to_upload_dir / "3_products_all.csv"
    combinations, stats, uncovered = build_combinations(
        export, SETTINGS.paths.attributes_csv, _backbone_paths(),
        products if products.exists() else None)
    path = SETTINGS.paths.to_upload_dir / "2_valid_combinations.csv"
    write_combinations(combinations, path)
    if uncovered:
        review = SETTINGS.paths.review_dir / "tree_uncovered_variations.csv"
        review.parent.mkdir(parents=True, exist_ok=True)
        with review.open("w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=["Key", "Id", "Name", "category"])
            w.writeheader()
            w.writerows(uncovered)
        stats["uncovered_review"] = str(review)
    log.info("valid combinations built", extra={"extra_fields": stats})
    return path
