"""Regenerate origin_map.csv from the master variety reference (stone_variants_with_finishes_raw.csv).

Origin (the stone's quarry country) is missing from every scrape, so we resolve it from a reference
keyed on the variety -- NOT the supplier (a trader in Italy sells Brazilian quartzite; the origin is
Brazil). The master raw file carries a `country` per variety, so this builds two tiers of rule:

  1. variety  -- exact (type-independent) name -> its primary country. Covers every KNOWN variety.
  2. pattern  -- a name TOKEN that, across the whole corpus, points overwhelmingly at one country
                 (e.g. 'carrara'->IT, 'persa'->BR, 'sesame'->CN). These GENERALISE: a brand-new
                 variety the exact tier has never seen still resolves if its name carries a known
                 stone-family / place token. This is what lets origin survive new variants.

A genuinely-new variety with no token match falls through derive_origin to the supplier default,
which is FLAGGED for review -- that flag is the maintenance loop: confirm it once and re-running this
builder (after the variety lands in the raw reference) promotes it to an exact rule. Re-run anytime:

    python -m stone_pipeline.reference.build_origin_map
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

from stone_pipeline.adapters.tokens import COLOR_TOKENS, TYPE_TOKENS
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.reference import loaders

RAW = SETTINGS.paths.workspace_root / "from_medusa" / "development" / "stone_variants_with_finishes_raw.csv"

# tokens that describe a stone's look/cut/grade, not its origin -- never promote these to patterns
_GENERIC = {w.lower() for w in COLOR_TOKENS + TYPE_TOKENS} | {
    "slab", "tile", "block", "polished", "honed", "leathered", "brushed", "natural", "dark", "light",
    "classic", "royal", "imperial", "extra", "premium", "new", "old", "super", "jumbo", "antique",
    "crystal", "stone", "golden", "silver", "grain", "sugar", "ice", "snow", "star", "fantasy",
}
# a token is a usable origin pattern only if it is this common AND this single-country
_PATTERN_MIN_COUNT = 8
_PATTERN_MIN_FRACTION = 0.85


def _norm(s: str) -> str:
    return " ".join((s or "").strip().casefold().split())


def _first_iso(country: str, codes: dict) -> str | None:
    """First resolvable country in a possibly multi-country cell ('Angola, Brazil' -> the primary)."""
    for part in re.split(r"[,/]", country or ""):
        iso = codes.get(_norm(part))
        if iso:
            return iso
    return None


def build(write: bool = True) -> dict:
    codes = loaders.load_country_codes()
    rows = list(csv.DictReader(RAW.open(encoding="utf-8-sig")))

    by_name: dict[str, Counter] = defaultdict(Counter)
    by_token: dict[str, Counter] = defaultdict(Counter)
    unresolved = 0
    for r in rows:
        iso = _first_iso(r.get("country", ""), codes)
        name = r.get("Variant", "").strip()
        if not iso:
            unresolved += 1 if (r.get("country") or "").strip() else 0
            continue
        by_name[name][iso] += 1
        for tok in re.findall(r"[a-z]+", name.lower()):
            if len(tok) >= 4 and tok not in _GENERIC:
                by_token[tok][iso] += 1

    variety_rules = [
        {"match_type": "variety", "pattern": name, "country_iso": c.most_common(1)[0][0], "city": "", "county": ""}
        for name, c in by_name.items()
    ]
    pattern_rules = []
    for tok, c in by_token.items():
        total = sum(c.values())
        top, n = c.most_common(1)[0]
        if total >= _PATTERN_MIN_COUNT and n / total >= _PATTERN_MIN_FRACTION:
            pattern_rules.append((total, {"match_type": "pattern", "pattern": tok,
                                          "country_iso": top, "city": "", "county": ""}))
    # longest, most-evidenced tokens first so the most specific pattern wins the substring scan
    pattern_rules.sort(key=lambda x: (-len(x[1]["pattern"]), -x[0]))
    pattern_rules = [r for _, r in pattern_rules]

    out = variety_rules + pattern_rules
    if write:
        path = SETTINGS.paths.origin_map_csv
        with Path(path).open("w", newline="", encoding="utf-8") as h:
            w = csv.DictWriter(h, fieldnames=["match_type", "pattern", "country_iso", "city", "county"])
            w.writeheader()
            w.writerows(out)
    return {"variety_rules": len(variety_rules), "pattern_rules": len(pattern_rules),
            "raw_varieties": len(rows), "unresolved": unresolved}


if __name__ == "__main__":
    stats = build(write=True)
    print(f"origin_map.csv rebuilt: {stats['variety_rules']} variety rules + "
          f"{stats['pattern_rules']} geographic patterns "
          f"({stats['raw_varieties']} raw varieties, {stats['unresolved']} unresolved)")
