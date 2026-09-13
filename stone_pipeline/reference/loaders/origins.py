"""Origins: the per-variety origin map (variety, type) -> countries, the per-supplier overrides, the country
name -> ISO-2 table, and the ONE country resolver shared by derive and the matcher."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from stone_pipeline.core.text import match_key
from stone_pipeline.reference.loaders.common import env, log, norm


# --- origin_map.csv -----------------------------------------------------------
@dataclass
class OriginRule:
    # One per-variety origin: (variety, stone_type) -> country/countries. Keyed by type so a homonym (Onyx vs
    # Granite "Aqua Blue") carries a different origin. The map IS the source of truth -- every row is
    # authoritative. country_iso may list MORE THAN ONE country (comma-separated, e.g. "BR,IN") when the same
    # trade name is quarried in several countries; derive picks the one matching the supplier's home country.
    variety: str
    country_iso: str
    city: str
    county: str
    stone_type: str = ""

    @property
    def countries(self) -> list[str]:
        """The ordered ISO-2 candidates parsed from country_iso ("BR,IN" -> ["BR","IN"]). A single-country
        row yields a 1-element list, so existing single-origin behaviour is unchanged. Duplicates are folded
        (order-preserving) so a stray "BR,BR" is treated as the single origin it means."""
        return list(dict.fromkeys(c.strip().upper() for c in self.country_iso.split(",") if c.strip()))


@dataclass
class OriginMap:
    rules: list[OriginRule] = field(default_factory=list)

    def _ensure_index(self) -> None:
        # lazily built (there can be 10k+ rules); invalidated by apply_origin_overlay. Last wins on a dup.
        if not hasattr(self, "_index"):
            self._index = {(norm(r.variety), norm(r.stone_type)): r for r in self.rules}

    def exact(self, name: str, stone_type: str | None = None) -> Optional[OriginRule]:
        """The (name, stone_type) rule, or None. STRICT: a homonym differs by type and there is no fallback,
        so a miss returns None (derive flags it) rather than a country guessed from a different type."""
        self._ensure_index()
        return self._index.get((norm(name), norm(stone_type or "")))

    def widen(self, widened: dict[tuple[str, str, str], str]) -> int:
        """ADD a country to a variety's documented origins ((source, name, type) -> ISO2, the operator's per-
        decision "add to documented origins"): a UNION with the rule's list, never a replacement, so a widen to
        one country can never take a stone's other origins away. A variety with no rule yet gets one. Returns
        the number of countries actually added."""
        if not widened:
            return 0
        by_key = {(norm(r.variety), norm(r.stone_type)): i for i, r in enumerate(self.rules)}
        added = 0
        for (_source, variety, stone_type), iso in widened.items():
            iso = (iso or "").strip().upper()
            if not variety or not iso or not (stone_type or "").strip():
                continue
            idx = by_key.get((norm(variety), norm(stone_type)))
            if idx is None:
                self.rules.append(OriginRule(variety=variety, country_iso=iso, city="", county="", stone_type=stone_type))
                by_key[(norm(variety), norm(stone_type))] = len(self.rules) - 1
                added += 1
            elif iso not in self.rules[idx].countries:
                rule = self.rules[idx]
                self.rules[idx] = OriginRule(variety=rule.variety, country_iso=",".join([*rule.countries, iso]),
                                             city=rule.city, county=rule.county, stone_type=rule.stone_type)
                added += 1
        if added and hasattr(self, "_index"):
            delattr(self, "_index")
        return added

    def apply_origin_overlay(self, minted: dict[tuple[str, str], str]) -> int:
        """Overlay operator-minted origins ((name, type) -> ISO2): effective map = CSV + mints. A mint
        REPLACES the CSV rule for that SAME (name, type) only; a type-less mint is skipped (can't emit).
        Returns the count added or overridden."""
        if not minted:
            return 0
        by_key = {(norm(r.variety), norm(r.stone_type)): i for i, r in enumerate(self.rules)}
        added = 0
        for (variety, stone_type), iso in minted.items():
            iso = (iso or "").strip().upper()
            if not variety or not iso or not (stone_type or "").strip():
                continue                         # type-less mint can't be type-scoped -> skip (never emits)
            rule = OriginRule(variety=variety, country_iso=iso, city="", county="", stone_type=stone_type)
            idx = by_key.get((norm(variety), norm(stone_type)))
            if idx is not None:
                self.rules[idx] = rule       # operator override wins over the CSV, for THIS (variety, type)
            else:
                self.rules.append(rule)
            added += 1
        if hasattr(self, "_index"):
            delattr(self, "_index")          # force a rebuild so the overlay is visible
        return added


def load_country_codes(path: Path | None = None) -> dict[str, str]:
    """country name (normalized) -> ISO-2, so a scraped 'India'/'Turkey' resolves."""
    path = Path(path or env().SETTINGS.paths.country_codes_csv)
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            name = norm(record.get("name") or "")
            iso = (record.get("iso2") or "").strip().upper()
            if name and iso:
                out[name] = iso
    return out


def load_origin_map(path: Path | None = None) -> OriginMap:
    # hand-maintained in catalog_source/
    path = Path(path) if path else env().SETTINGS.paths.origin_map_csv
    origin = OriginMap()
    if not path.exists():
        # NOT silent: a missing map degrades EVERY origin to supplier-default/unresolved -- loud so it
        # surfaces in CloudWatch instead of shipping a catalog with no real origins.
        log.warning("origin_map missing -- all origins fall back to supplier default / unresolved",
                    extra={"extra_fields": {"path": str(path)}})
        return origin
    seen: dict[tuple[str, str], str] = {}  # (norm variety, norm type) -> iso, to catch conflicting dups
    skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            variety = (record.get("variety") or "").strip()
            iso = (record.get("country_iso") or "").strip().upper()
            st = (record.get("stone_type") or "").strip()
            # A usable rule needs a variety name, a country, AND a type -- origin is keyed by (name, type).
            # A row missing any is skipped (blank country/type is a TODO to fill, not a rule to emit).
            if not variety or not iso or not st:
                if variety or iso:
                    skipped += 1
                continue
            key = (norm(variety), norm(st))
            if key in seen and seen[key] != iso:
                log.warning("origin_map duplicate (variety, type) with conflicting country (last wins)",
                            extra={"extra_fields": {"variety": variety, "stone_type": st,
                                                    "iso": iso, "prior": seen[key]}})
            seen[key] = iso
            origin.rules.append(OriginRule(variety=variety, country_iso=iso,
                                           city=(record.get("city") or "").strip(),
                                           county=(record.get("county") or "").strip(), stone_type=st))
    if skipped:
        log.warning("origin_map rows skipped (need variety + country_iso + stone_type)",
                    extra={"extra_fields": {"skipped": skipped, "path": str(path)}})
    return origin


# --- origin_supplier_overrides.csv -------------------------------------------
@dataclass
class OriginOverrides:
    # (source, variety, stone_type) -> ISO2. A curated per-SUPPLIER origin for a stone a supplier SELLS but
    # does NOT quarry in its home country (a reseller/trader). Scoped to ONE source, so it never affects
    # another supplier of the same variety. This is the resolution of an origin_multi_home_absent review
    # where the supplier's stone is one of the KNOWN candidate origins (not a new quarry country).
    rules: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def lookup(self, source: str, variety: str, stone_type: str | None) -> Optional[str]:
        return self.rules.get((norm(source), norm(variety), norm(stone_type or "")))

    def apply_overlay(self, decisions: dict[tuple[str, str, str], str]) -> int:
        """Overlay operator-confirmed per-vendor origins ((norm source, norm variety, norm type) -> ISO)
        onto the CSV-loaded rules -- a confirmation from the origin review queue REPLACES any CSV rule for
        the same key, and derive then resolves it at the supplier_override tier. Returns the count applied."""
        n = 0
        for key, iso in decisions.items():
            iso = (iso or "").strip().upper()
            if len(key) == 3 and all(key) and iso:
                self.rules[key] = iso
                n += 1
        return n


def load_origin_overrides(path: Path | None = None) -> OriginOverrides:
    path = Path(path) if path else env().SETTINGS.paths.origin_overrides_csv
    ov = OriginOverrides()
    if not path.exists():
        return ov                                    # optional file: no overrides is the normal case
    skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            source = (record.get("source") or "").strip()
            variety = (record.get("variety") or "").strip()
            st = (record.get("stone_type") or "").strip()
            iso = (record.get("country_iso") or "").strip().upper()
            # A usable override needs all four -- source + variety + type identify the row, iso is the answer.
            if not (source and variety and st and iso):
                if source or variety:
                    skipped += 1
                continue
            ov.rules[(norm(source), norm(variety), norm(st))] = iso
    if skipped:
        log.warning("origin overrides rows skipped (need source + variety + stone_type + country_iso)",
                    extra={"extra_fields": {"skipped": skipped, "path": str(path)}})
    return ov


def resolve_iso(value: str, country_codes: dict[str, str], valid_iso_codes) -> str | None:
    """Resolve a country NAME or alias ('India', 'UK'->GB) via country_codes FIRST, then accept a bare
    2-letter token only if it is a real ISO-3166 alpha-2 code. Name-first so a common alias like 'UK' maps
    to GB instead of short-circuiting to the (invalid) literal 'UK'; the ISO-set check rejects a bogus
    'XX'/'EN' rather than passing it through as a confident country. The ONE country resolver, shared by
    derive (the product's origin) and the matcher (its origin evidence)."""
    v = (value or "").strip()
    if not v:
        return None
    hit = country_codes.get(match_key(v))   # same key the country table was built with (norm)
    if hit:
        return hit
    if len(v) == 2 and v.isalpha() and v.upper() in valid_iso_codes:
        return v.upper()
    return None
