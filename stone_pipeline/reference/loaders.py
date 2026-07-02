"""Reference data loaders (section 6, section 6A).

Three files do three jobs (section 6A):
  - backbone.json decides what is valid (per variety: stone_type and the allowed
    colour, finish, quality sets). Names only, no ids.
  - attributes.csv maps attribute names to backend ids (colour, finish, quality,
    type, and the two category pcat ids).
  - variants_<category>.csv maps a variety name (plus aliases) to its variation
    id, selected by branch (slabs vs blocks).

Every id-bearing file is a live backend export and can lag (section 6). A
content hash of each is recorded so a run is reproducible against a snapshot.
The loaders return typed structures; gap detection and id validity are checked
against the live set at run start (the fingerprint check below).
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Optional

from stone_pipeline.config.settings import CATEGORIES, SETTINGS
from stone_pipeline.adapters.tokens import explicit_type_word
from stone_pipeline.core import logfmt
from stone_pipeline.core.manifest import content_hash
from stone_pipeline.core.text import ascii_fold, looks_code_shaped, match_key

log = logfmt.get_logger("reference.loaders")


_TYPE_SLUGS: Optional[set[str]] = None


def _type_slugs() -> set[str]:
    """All backend type slugs (incl. multi-word 'Dolomite Marble' -> 'dolomite_marble' and the
    Sodalite->Sodalite Syenite synonym targets), cached. Lets is_mistyped_variant find where the
    Key's type ends instead of assuming it is a single segment."""
    global _TYPE_SLUGS
    if _TYPE_SLUGS is None:
        from stone_pipeline.adapters.tokens import TYPE_SYNONYMS
        names = [v for v, _ in load_attributes().by_category.get("type", {}).values()]
        names += list(TYPE_SYNONYMS.values())
        # slugify the SAME way gen_key encodes a Key's type ([^a-z0-9]+ -> '_'), so a multi-word OR
        # hyphenated type ('Semi-Precious Stone' -> 'semi_precious_stone') matches the underscore-keyed
        # Key. Joining on whitespace alone kept the hyphen ('semi-precious_stone') and never matched.
        _TYPE_SLUGS = {re.sub(r"[^a-z0-9]+", "_", _norm(n)).strip("_") for n in names if n}
    return _TYPE_SLUGS


def type_slug_from_key(key: str) -> str:
    """The stone-type SLUG embedded in a variant Key, MULTI-WORD aware: the longest known type slug
    that == or prefixes the post-branch portion -- 'slab_dolomite_marble_angelus_..' -> 'dolomite_marble'
    (not the truncated 'dolomite'), 'slab_semi_precious_stone_..' -> 'semi_precious_stone'. Falls back to
    the single token after the branch when nothing matches (a brand-new/unknown type)."""
    parts = (key or "").split("_")
    if len(parts) < 2:
        return ""
    after_branch = "_".join(parts[1:])
    return max((t for t in _type_slugs() if after_branch == t or after_branch.startswith(t + "_")),
               key=len, default=parts[1].casefold())


def is_mistyped_variant(key: str, name: str) -> bool:
    """An existing variant whose NAME carries an unambiguous stone-type word NOT present in its own
    Key's type is mis-typed -- e.g. 'Azul White Quartzite' under a slab_ONYX_ key. Listed for
    deletion so the cleaning flow re-mints it correctly. The Key's type may be MULTI-WORD
    ('slab_dolomite_marble_...'), so we match the longest known type slug that prefixes the Key's
    post-branch portion rather than taking parts[1] (which falsely flagged 'Dolomite Marble' /
    'Sodalite Syenite' variants named with their second word)."""
    ntw = explicit_type_word(name)
    parts = (key or "").split("_")
    if not ntw or len(parts) < 3:
        return False
    key_type = type_slug_from_key(key)   # multi-word aware (shared with the variation index)
    # Resolve the name's type word to its CANONICAL type before comparing (the same synonym map
    # resolve_id uses), so 'Agata' -> 'Agate' and 'Sodalite' -> 'Sodalite Syenite' are judged against
    # the canonical, not the literal foreign spelling -- else 'Agata Blue' keyed 'agate' would falsely
    # flag ('agata' != 'agate'). Mis-typed iff the canonical type shares NO token with the Key's type
    # (token overlap, so a name 'Marble' is consistent with a 'dolomite_marble' Key).
    from stone_pipeline.adapters.tokens import TYPE_SYNONYMS
    canonical = TYPE_SYNONYMS.get(_norm(ntw), ntw)
    return set(_norm(canonical).split()).isdisjoint(key_type.split("_"))

log = logfmt.get_logger("reference")

# Vocabulary categories carried in attributes.csv (section 6).
VOCAB_CATEGORIES = ("color", "finish", "type", "quality")


def _norm(value: str) -> str:
    """The shared normalization for every name lookup -> delegates to core.text.match_key: ascii-fold
    accents, casefold, AND fold every separator run (space/underscore/hyphen/dash/slash) to one space.
    Folding separators too is what lets a hyphenated vocab value ('Semi-Precious Stone') match an
    underscore/space slug, so a punctuation difference never splits the same name. Accent folding
    keeps backbone lookups consistent with the variation index, so 'Porrino' == 'Porriño'."""
    return match_key(value)


# --- attributes.csv -----------------------------------------------------------
@dataclass
class Attributes:
    """Name to backend id, per vocabulary, plus the two category pcat ids."""

    # category -> {normalized_name -> (canonical_name, id)}
    by_category: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    category_pcat: dict[str, str] = field(default_factory=dict)  # 'Slabs'/'Blocks' -> pcat id

    def resolve_id(self, category: str, name: str) -> Optional[tuple[str, str]]:
        """Return (canonical_name, id) for an exact normalized name, else None. For types, a synonym
        ('Sodalite' -> 'Sodalite Syenite') falls back to the canonical attribute so a commercial/short
        name resolves without polluting the type vocabulary itself."""
        table = self.by_category.get(category, {})
        hit = table.get(_norm(name))
        if hit is not None:
            return hit
        if category == "type":
            from stone_pipeline.adapters.tokens import TYPE_SYNONYMS
            canonical = TYPE_SYNONYMS.get(_norm(name))
            if canonical:
                return table.get(_norm(canonical))
        return None

    def canonical_names(self, category: str) -> list[str]:
        return [canon for canon, _ in self.by_category.get(category, {}).values()]

    def all_ids(self) -> list[str]:
        ids: list[str] = list(self.category_pcat.values())
        for table in self.by_category.values():
            ids.extend(i for _, i in table.values())
        return ids


def load_attributes(path: Path | None = None) -> Attributes:
    path = Path(path or SETTINGS.paths.attributes_csv)
    attrs = Attributes()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            category = (record.get("category") or "").strip()
            value = (record.get("value") or "").strip()
            sourceid = (record.get("sourceid") or "").strip()
            if not category or not value or not sourceid:
                continue
            if category == "category":
                attrs.category_pcat[value] = sourceid
            else:
                attrs.by_category.setdefault(category, {})[_norm(value)] = (value, sourceid)
    return attrs


# --- variants_<category>.csv --------------------------------------------------
@dataclass
class Variant:
    variation_id: str
    key: str
    name: str
    image: str
    aliases: list[str]


@dataclass
class VariantTable:
    branch: str  # 'slab' or 'block'
    by_id: dict[str, Variant] = field(default_factory=dict)
    # normalized name/alias -> variation_id (built here for exact lookups; the
    # full projection index is built in matching/index.py for M5).
    surface_to_id: dict[str, str] = field(default_factory=dict)

    def all_ids(self) -> list[str]:
        return list(self.by_id.keys())


def _delete_keys() -> set[str]:
    """Keys flagged in variants_to_delete -- excluded from matching so a junk/phantom variant on its
    way out can't intercept a product match or a fold-in (e.g. a phantom 'White Super ES' must not
    capture the scrape now that it folds into 'Super White')."""
    p = SETTINGS.paths.review_dir / "variants_to_delete.csv"
    if not p.exists():
        return set()
    return {(r.get("Key") or "").strip() for r in csv.DictReader(p.open(encoding="utf-8-sig"))
            if (r.get("Key") or "").strip()}


def load_variants(path: Path, branch: str, key_prefix: str | None = None) -> VariantTable:
    """Load a variants table. key_prefix filters rows by their Key's leading token
    (slab/block), so a single combined export can feed both branches: slab-keyed
    rows go to the slab table, block-keyed rows to the block table."""
    table = VariantTable(branch=branch)
    delete_keys = _delete_keys()
    path = Path(path)
    if not path.exists():
        log.warning(f"variants file absent for branch {branch}: {path}")
        return table
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            vid = (record.get("Id") or "").strip()
            name = (record.get("Name") or "").strip()
            key = (record.get("Key") or "").strip()
            if not vid or not name:
                continue
            # Never match products to a JUNK existing variant -- a bare-code one ('Mgt','Gs', a
            # supplier code/brand abbreviation wrongly minted) OR a mis-typed one ('Azul White
            # Quartzite' under a slab_ONYX_ key). Both are surfaced in review/<env>/variants_to_delete.csv
            # for deletion from Medusa; the cleaning flow re-mints the mis-typed ones correctly.
            if looks_code_shaped(name) == "bare_code" or is_mistyped_variant(key, name) or key in delete_keys:
                continue
            if key_prefix and not key.casefold().startswith(key_prefix.casefold()):
                continue
            aliases_raw = (record.get("Aliases") or "").strip()
            aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()] if aliases_raw else []
            variant = Variant(
                variation_id=vid,
                key=(record.get("Key") or "").strip(),
                name=name,
                image=(record.get("Image") or "").strip(),
                aliases=aliases,
            )
            table.by_id[vid] = variant
            # NOTE: surface_to_id is intentionally NOT populated here -- production matching builds its
            # own exact index in matching/index.py; this per-row normalize+insert over ~24k variants was
            # pure wasted work (the field stays for the test that clears it).
    return table


# --- backbone.json ------------------------------------------------------------
@dataclass
class BackboneVariety:
    variant: str
    category: str
    stone_type: str
    colors: list[str]
    finishes: list[str]
    qualities: list[str]
    aliases: list[str]


@dataclass
class Backbone:
    # A name can repeat across stone types and colours (e.g. "Green" appears as
    # several distinct varieties), so each key holds a list and disambiguation is
    # by stone_type (section 5A blocking).
    by_norm_name: dict[str, list[BackboneVariety]] = field(default_factory=dict)
    by_norm_alias: dict[str, list[BackboneVariety]] = field(default_factory=dict)

    def lookup_all(self, name: str) -> list[BackboneVariety]:
        key = _norm(name)
        return self.by_norm_name.get(key) or self.by_norm_alias.get(key) or []

    def lookup(self, name: str, stone_type: str | None = None) -> Optional[BackboneVariety]:
        candidates = self.lookup_all(name)
        if not candidates:
            return None
        if stone_type:
            for variety in candidates:
                if _norm(variety.stone_type) == _norm(stone_type):
                    return variety
            # an explicit type was requested but NO same-name candidate has it: do NOT silently return
            # a FOREIGN-type variety (its colours/finishes/validity would be checked against the wrong
            # stone). Let the caller decide -- reconcile falls back to an untyped lookup, the variation
            # index uses the Key-derived type.
            return None
        return candidates[0]

    def is_valid_leaf(self, variety: BackboneVariety, color: str, finish: str, quality: str) -> bool:
        """Set-membership validity (section 6A): a PRESENT colour, finish, or quality must each be in
        the variety's allowed set (an empty value passes). Names compared normalized so case never
        causes a false gap."""
        cset = {_norm(c) for c in variety.colors}
        fset = {_norm(f) for f in variety.finishes}
        qset = {_norm(q) for q in variety.qualities}
        return (
            (not color or _norm(color) in cset)
            and (not finish or _norm(finish) in fset)
            and (not quality or _norm(quality) in qset)
        )

    def __len__(self) -> int:
        return len(self.by_norm_name)


def _split_aliases(raw_aliases: list) -> list[str]:
    """Backbone aliases are a list of strings, some containing comma-joined
    surface forms (e.g. 'Preto Agata, Agate Black'). Flatten into single forms."""
    out: list[str] = []
    for entry in raw_aliases or []:
        if not isinstance(entry, str):
            continue
        for piece in entry.split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def load_backbone(path: Path | None = None) -> Backbone:
    path = Path(path or SETTINGS.paths.backbone_json)
    backbone = Backbone()
    data = json.loads(path.read_text(encoding="utf-8"))
    posts = data.get("posts", data if isinstance(data, list) else [])
    for post in posts:
        variant_name = (post.get("variant") or "").strip()
        if not variant_name:
            continue
        variety = BackboneVariety(
            variant=variant_name,
            category=(post.get("category") or "").strip(),
            stone_type=(post.get("stone_type") or "").strip(),
            colors=[c for c in (post.get("color") or []) if c],
            finishes=[f for f in (post.get("finishes") or []) if f],
            qualities=[q for q in (post.get("qualities") or []) if q],
            aliases=_split_aliases(post.get("aliases") or []),
        )
        backbone.by_norm_name.setdefault(_norm(variant_name), []).append(variety)
        for alias in variety.aliases:
            backbone.by_norm_alias.setdefault(_norm(alias), []).append(variety)
    return backbone


# --- ports.csv ----------------------------------------------------------------
@dataclass
class Ports:
    by_country: dict[str, list[str]] = field(default_factory=dict)  # iso2 -> [port_id]
    iso_by_port: dict[str, str] = field(default_factory=dict)        # port_id -> iso2
    by_name: dict[str, str] = field(default_factory=dict)            # norm(name) -> port_id
    by_locode: dict[str, str] = field(default_factory=dict)          # UN/LOCODE -> port_id

    def for_country(self, iso2: str, limit: int = 2) -> list[str]:
        return self.by_country.get((iso2 or "").strip().upper(), [])[:limit]

    def country_of(self, port_id: str | None) -> str | None:
        return self.iso_by_port.get((port_id or "").strip())

    def resolve(self, token: str) -> str | None:
        """Resolve a sources.yaml port entry to a port id: a known id passes through,
        else a UN/LOCODE ('ITBDS') or a port name ('Brindisi') from ports.csv."""
        t = (token or "").strip()
        if t in self.iso_by_port:
            return t
        return self.by_locode.get(t.upper()) or self.by_name.get(_norm(t))

    def all_ids(self) -> list[str]:
        ids: list[str] = []
        for lst in self.by_country.values():
            ids.extend(lst)
        return ids


def load_ports(path: Path | None = None) -> Ports:
    """ports.csv is supplied by the user into catalog_source; fall back to the
    reference stub if absent (section 6, section 3.3)."""
    candidate = Path(path) if path else SETTINGS.paths.ports_csv
    if not candidate.exists():
        candidate = SETTINGS.paths.ports_csv_fallback
    ports = Ports()
    if not candidate.exists():
        log.warning("ports.csv absent; origin->ports resolution will fall back to default")
        return ports
    with candidate.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            pid = (record.get("port_id") or record.get("id") or record.get("Id") or "").strip()
            if not pid:
                continue
            iso = (record.get("country_iso") or "").strip().upper()
            if iso:
                ports.by_country.setdefault(iso, []).append(pid)
                ports.iso_by_port[pid] = iso
            if name := (record.get("name") or "").strip():       # for resolving by name
                ports.by_name[_norm(name)] = pid
            if locode := (record.get("un_locode") or "").strip().upper():
                ports.by_locode[locode] = pid
    return ports


# --- units.csv ----------------------------------------------------------------
@dataclass
class UnitEntry:
    dimension: str
    canonical: str
    factor: float


@dataclass
class Units:
    by_token: dict[str, UnitEntry] = field(default_factory=dict)

    def convert(self, value: float, token: str) -> Optional[float]:
        entry = self.by_token.get((token or "").strip().casefold())
        if entry is None:
            return None
        return value * entry.factor


def load_units(path: Path | None = None) -> Units:
    path = Path(path or SETTINGS.paths.units_csv)
    units = Units()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            token = (record.get("token") or "").strip().casefold()
            if not token:
                continue
            units.by_token[token] = UnitEntry(
                dimension=(record.get("dimension") or "").strip(),
                canonical=(record.get("canonical") or "").strip(),
                factor=float(record.get("factor") or 1.0),
            )
    return units


# --- synonyms/<vocab>.csv -----------------------------------------------------
def load_synonyms(vocab: str, directory: Path | None = None) -> dict[str, str]:
    """raw (normalized) -> canonical backend value, or 'none' to resolve to no id
    without an error (section 7, Stage 3). Missing file is an empty map."""
    directory = Path(directory or SETTINGS.paths.synonyms_dir)
    path = directory / f"{vocab}.csv"
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            raw = _norm(record.get("raw") or "")
            canon = (record.get("canonical") or "").strip()
            if raw and canon:
                mapping[raw] = canon
    return mapping


# --- origin_map.csv -----------------------------------------------------------
@dataclass
class OriginRule:
    match_type: str  # 'variety' or 'pattern'
    pattern: str
    country_iso: str
    city: str
    county: str


@dataclass
class OriginMap:
    rules: list[OriginRule] = field(default_factory=list)

    def lookup(self, name: str) -> Optional[OriginRule]:
        # exact variety match first (O(1) via a lazily-built index -- there can be 10k+ rules), then
        # a name PATTERN. Patterns are single tokens (e.g. 'oman', 'persa') and MUST match a whole
        # word, never a substring -- else 'oman' matches "rOMANo" (Italian travertine -> Oman) and
        # 'india' matches "INDIAna" (US limestone -> India). Tokenize with the same [a-z]+ rule the
        # builder uses, so pattern membership is exact. The index is cached after the first call.
        if not hasattr(self, "_variety_index"):
            self._variety_index = {_norm(r.pattern): r for r in self.rules if r.match_type == "variety"}
            # MOST-SPECIFIC pattern wins, deterministically: longest token first, then alphabetic --
            # so when a name carries two pattern tokens the result never depends on CSV row order.
            self._pattern_rules = sorted(
                ((_norm(r.pattern), r) for r in self.rules if r.match_type == "pattern"),
                key=lambda pr: (-len(pr[0]), pr[0]),
            )
        norm_name = _norm(name)
        hit = self._variety_index.get(norm_name)
        if hit is not None:
            return hit
        tokens = set(re.findall(r"[a-z]+", norm_name))
        for pat, rule in self._pattern_rules:
            if pat in tokens:
                return rule
        return None


def load_country_codes(path: Path | None = None) -> dict[str, str]:
    """country name (normalized) -> ISO-2, so a scraped 'India'/'Turkey' resolves."""
    path = Path(path or SETTINGS.paths.country_codes_csv)
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            name = _norm(record.get("name") or "")
            iso = (record.get("iso2") or "").strip().upper()
            if name and iso:
                out[name] = iso
    return out


def load_origin_map(path: Path | None = None) -> OriginMap:
    # hand-maintained in catalog_source/; fall back to the reference stub only if it actually exists.
    path = Path(path) if path else SETTINGS.paths.origin_map_csv
    if not path.exists() and SETTINGS.paths.origin_map_csv_fallback.exists():
        path = SETTINGS.paths.origin_map_csv_fallback
    origin = OriginMap()
    if not path.exists():
        # NOT silent: a missing map degrades EVERY origin to supplier-default/unresolved -- loud so it
        # surfaces in CloudWatch instead of shipping a catalog with no real origins.
        log.warning("origin_map missing -- all origins fall back to supplier default / unresolved",
                    extra={"extra_fields": {"path": str(path)}})
        return origin
    seen: dict[str, str] = {}  # normalized variety name -> iso, to catch conflicting duplicates
    skipped = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            mt = (record.get("match_type") or "").strip().casefold()
            pat = (record.get("pattern") or "").strip()
            iso = (record.get("country_iso") or "").strip().upper()
            # A usable rule needs a known kind, a pattern, AND a country -- a row missing any of these
            # must NOT become a hit that stamps a blank/garbage country at medium confidence.
            if mt not in ("variety", "pattern") or not pat or not iso:
                if pat or iso or record.get("match_type"):
                    skipped += 1
                continue
            if mt == "variety":
                prior = seen.get(_norm(pat))
                if prior and prior != iso:
                    log.warning("origin_map duplicate variety with conflicting country (last wins)",
                                extra={"extra_fields": {"variety": pat, "iso": iso, "prior": prior}})
                seen[_norm(pat)] = iso
            origin.rules.append(OriginRule(match_type=mt, pattern=pat, country_iso=iso,
                                           city=(record.get("city") or "").strip(),
                                           county=(record.get("county") or "").strip()))
    if skipped:
        log.warning("origin_map rows skipped (need match_type variety|pattern + pattern + country_iso)",
                    extra={"extra_fields": {"skipped": skipped, "path": str(path)}})
    return origin


# --- the bundle of all reference data + version pinning -----------------------
@dataclass
class ReferenceData:
    attributes: Attributes
    # category name -> variant table, for categories that share the stone-variety
    # vocabulary (slab/block/tile). One per registry entry; the named accessors
    # below are kept for existing callers/tests.
    variants: dict[str, VariantTable]
    backbone: Backbone
    ports: Ports
    units: Units
    origin_map: OriginMap
    country_codes: dict[str, str]   # country name (normalized) -> ISO-2
    synonyms: dict[str, dict[str, str]]
    versions: dict[str, str]
    overrides: object = None  # state.overrides.Overrides; lazy import to avoid cycle

    @cached_property
    def valid_iso_codes(self) -> frozenset:
        """The real ISO-3166 alpha-2 codes we know (the values of country_codes), so a bare 2-letter
        scraped token can be VALIDATED rather than blindly trusted (rejects 'XX'; lets 'UK' resolve
        to GB via the name/alias path instead of passing through as a bogus code)."""
        return frozenset(self.country_codes.values())

    @property
    def variants_slabs(self) -> VariantTable:
        return self.variants["slab"]

    @property
    def variants_blocks(self) -> VariantTable:
        return self.variants["block"]

    @property
    def variants_tiles(self) -> VariantTable:
        return self.variants["tile"]


def load_all() -> ReferenceData:
    from stone_pipeline.state.overrides import load_overrides

    paths = SETTINGS.paths
    ref = ReferenceData(
        attributes=load_attributes(),
        # ONE combined export for every category; the category is the Key prefix, so
        # the same file is split into a per-category index, one per registry entry
        # that shares the stone-variety vocabulary.
        variants={c.name: load_variants(paths.export_file, c.name, key_prefix=c.name)
                  for c in CATEGORIES if c.shares_variety_vocab},
        backbone=load_backbone(),
        ports=load_ports(),
        units=load_units(),
        origin_map=load_origin_map(),
        country_codes=load_country_codes(),
        synonyms={v: load_synonyms(v) for v in VOCAB_CATEGORIES},
        overrides=load_overrides(),
        versions={
            "attributes": content_hash(paths.attributes_csv),
            "variants_export": content_hash(paths.export_file),
            "backbone": content_hash(paths.backbone_json),
            "ports": content_hash(
                paths.ports_csv if paths.ports_csv.exists() else paths.ports_csv_fallback
            ),
            "units": content_hash(paths.units_csv),
            "origin_map": content_hash(
                paths.origin_map_csv if paths.origin_map_csv.exists() else paths.origin_map_csv_fallback
            ),
        },
    )
    log.info(
        "reference loaded",
        extra={
            "extra_fields": {
                "attributes_colors": len(ref.attributes.by_category.get("color", {})),
                "variants_slabs": len(ref.variants_slabs.by_id),
                "backbone_varieties": len(ref.backbone),
            }
        },
    )
    return ref
