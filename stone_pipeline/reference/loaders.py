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
from functools import cached_property, lru_cache
from pathlib import Path
from typing import Optional

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import CATEGORIES, IS_PRODUCTION, SETTINGS
from stone_pipeline.adapters.tokens import explicit_type_word
from stone_pipeline.core import logfmt
from stone_pipeline.core.numbers import parse_number
from stone_pipeline.core.manifest import content_hash
from stone_pipeline.core.text import ascii_fold, looks_code_shaped, match_key


_TYPE_SLUGS: Optional[set[str]] = None


def _type_slugs() -> set[str]:
    """All backend type slugs (incl. multi-word 'Dolomite Marble' -> 'dolomite_marble' and the
    Sodalite->Sodalite Syenite synonym targets), cached. Lets is_mistyped_variant find where the
    Key's type ends instead of assuming it is a single segment."""
    global _TYPE_SLUGS
    if _TYPE_SLUGS is None:
        names = [v for v, _ in load_attributes().by_category.get("type", {}).values()]
        names += list(load_synonyms("type").values())
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


# -- variety identity: the ONE rule the whole pipeline dedups by ---------------
# A variety is (branch, stone-type, name). Two Keys with the same identity are the SAME variety twice --
# the class that ships duplicate products (a legacy Key whose name-slug baked in an alias:
# `..._verde_ubatuba_ubatuba_...` vs the clean `..._verde_ubatuba_...`). Type comes from type_slug_from_key
# (multi-word aware, so `sodalite_syenite` is one type and a genuine multi-type homonym like Aqua Blue as
# gneiss/granite/marble/onyx stays distinct). Name is the DISPLAY Name accent-folded + slugged, so an
# alias in a legacy Key's slug never splits the variety. This single function is used by emit (collapse
# before write), the matcher reference (exclude non-survivors), and the ledger reconcile -- one identical
# rule everywhere, so a dup can never enter from any path.

def _name_slug(name: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", ascii_fold(name or "").casefold())).strip("_")


def variety_identity(key: str, name: str) -> tuple[str, str, str]:
    """(branch, type-slug, folded name-slug) -- the dedup identity of a variety. See the block comment."""
    return (key.split("_", 1)[0] if key else "", type_slug_from_key(key), _name_slug(name))


def _key_name_slug(key: str) -> str:
    """The name-slug portion of a Key (between the type and the trailing uuid) -- for survivor choice."""
    parts = key.split("_")
    uuid_at = next((i for i, p in enumerate(parts) if "-" in p), len(parts))
    return "_".join(parts[2:uuid_at])


def survivor_of(keys, seed_keys) -> str:
    """The one Key to keep for an identity group: prefer a committed-seed Key, then the cleanest name-slug
    (shortest -- an alias-laden legacy slug loses), then lexical. Deterministic and stable across runs."""
    return min(keys, key=lambda k: (k not in seed_keys, len(_key_name_slug(k)), k))


@lru_cache(maxsize=1)
def pristine_seed_keys() -> frozenset:
    """The Key set of the committed CLEAN seed -- the survivor authority for the identity collapse. Read
    from the read-only baked pristine copy; falls back to the live base (which IS the committed seed on a
    clean checkout). Empty if neither exists (collapse then just prefers the clean-slug Key)."""
    for p in (SETTINGS.paths.variants_export_base_seed_csv, SETTINGS.paths.variants_export_base_csv):
        if p.exists():
            with p.open(encoding="utf-8-sig", newline="") as h:
                return frozenset((r.get("Key") or "").strip()
                                 for r in csv.DictReader(h) if (r.get("Key") or "").strip())
    return frozenset()


def collapse_to_survivors(rows, seed_keys=None, key_col="Key", name_col="Name",
                          alias_col="Aliases", image_col="Image"):
    """Collapse every `variety_identity` group in `rows` to ONE survivor: fold the losers' name + aliases
    into the survivor (nothing searchable lost) and inherit a loser's image when the survivor has none,
    then drop the losers. Order-preserving on survivors. This is the load-bearing dedup -- run on the
    emitted variant set it makes the whole catalog a fixed point, so a duplicate can never ship regardless
    of a dirty base or a stale live export."""
    seed_keys = pristine_seed_keys() if seed_keys is None else seed_keys
    groups: dict = {}
    for r in rows:
        groups.setdefault(variety_identity(r.get(key_col, ""), r.get(name_col, "")), []).append(r)
    survivors: dict = {}
    for grp in groups.values():
        if len(grp) == 1:
            survivors[grp[0][key_col]] = grp[0]
            continue
        keep_key = survivor_of([r[key_col] for r in grp], seed_keys)
        keep = dict(next(r for r in grp if r[key_col] == keep_key))
        aliases = [a for a in (keep.get(alias_col) or "").split("|") if a]
        seen = {a.casefold() for a in aliases}
        own = (keep.get(name_col) or "").casefold()
        for r in grp:
            if r[key_col] == keep_key:
                continue
            for a in [r.get(name_col)] + (r.get(alias_col) or "").split("|"):
                a = (a or "").strip()
                if a and a.casefold() != own and a.casefold() not in seen:
                    aliases.append(a)
                    seen.add(a.casefold())
            if not (keep.get(image_col) or "").strip() and (r.get(image_col) or "").strip():
                keep[image_col] = r[image_col]
        keep[alias_col] = "|".join(aliases)
        survivors[keep_key] = keep
    out, emitted = [], set()
    for r in rows:                       # preserve original order on the surviving keys
        k = r[key_col]
        if k in survivors and k not in emitted:
            out.append(survivors[k])
            emitted.add(k)
    return out


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
    canonical = load_synonyms("type").get(_norm(ntw), ntw)
    return set(_norm(canonical).split()).isdisjoint(key_type.split("_"))

log = logfmt.get_logger("reference")

# Vocabulary categories carried in attributes.csv (section 6), declared by the active product-domain pack
# (the stone pack reproduces the historical set). Used only to load the per-attribute synonyms, so order
# is immaterial.
VOCAB_CATEGORIES = tuple(active_pack().attributes)


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
            canonical = load_synonyms("type").get(_norm(name))
            if canonical:
                return table.get(_norm(canonical))
        return None

    def canonical_names(self, category: str) -> list[str]:
        return [canon for canon, _ in self.by_category.get(category, {}).values()]

    def all_ids(self) -> list[str]:
        return list(self.category_pcat.values()) + [
            i for table in self.by_category.values() for _, i in table.values()]


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
                table = attrs.by_category.setdefault(category, {})
                nk = _norm(value)
                # Fail loud on a CONFLICTING duplicate: two names in a category that collapse to the same
                # normalized key but carry DIFFERENT Medusa ids. The value->id map silently last-wins
                # otherwise, so one id would shadow the other (and _norm folds accents/separators, so two
                # distinct raw names can collide). Names are unique per category today, so this never fires;
                # it surfaces a future Medusa dup instead of shipping the wrong id. A same-id repeat is
                # harmless (idempotent) and allowed.
                if nk in table and table[nk][1] != sourceid:
                    raise ValueError(
                        f"attributes.csv: {category} values {table[nk][0]!r} and {value!r} normalize to the "
                        f"same key {nk!r} but map to different ids ({table[nk][1]} vs {sourceid}); attribute "
                        f"names must be unique per category so the value->id mapping is unambiguous")
                table[nk] = (value, sourceid)
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
    # CRITICAL: a RETIRED variety must not be a resolution target here either. The matcher stamps a
    # product's variation_key from THIS reference (built off the lagging Medusa export, which still lists a
    # retired-but-not-yet-deleted variety), and the catalog-side surface exclusion runs too late to undo
    # that stamp. Excluding retired keys at the matcher (like delete_keys) is what actually stops a product
    # re-linking onto a retiring Key (which would FK-fail the eventual ack-done). One durable source: config.db.
    from stone_pipeline.stages import decisions
    delete_keys = _delete_keys() | decisions.load_retired()
    path = Path(path)
    if not path.exists():
        log.warning(f"variants file absent for branch {branch}: {path}")
        return table
    records = list(csv.DictReader(path.open(newline="", encoding="utf-8-sig")))
    # DEDUP the reference to survivors only: the live Medusa export can still list a re-key OLD SIDE that
    # is being deleted. If the matcher can stamp that Key onto a product, the product re-links onto a
    # variety emit will have collapsed away (an orphan / FK-fail on ack). Exclude every non-survivor of a
    # variety_identity group here, using the SAME survivor rule emit uses, so the matcher and emit agree on
    # exactly one Key per variety. Homonyms (different type) are separate identities and all survive.
    _seed = pristine_seed_keys()
    # Pick the survivor over ELIGIBLE keys only. survivor_of is blind to the exclusion predicates below
    # (bare_code / delete_keys), so if it picked an INELIGIBLE key (e.g. a re-key OLD SIDE that
    # is in delete_keys) every eligible key of that identity would be dropped as a dup_loser AND the chosen
    # survivor dropped by its own exclusion -- the whole variety vanishes from the matcher reference, so a
    # scrape for it matches nothing and MINTS A DUPLICATE (the exact orphan dup-losing was meant to prevent).
    # Excluding ineligible keys before grouping guarantees a good loser is promoted whenever one exists.
    def _eligible(key: str, name: str) -> bool:
        return not (looks_code_shaped(name) == "bare_code" or key in delete_keys)
    _by_identity: dict = {}
    for rec in records:
        k = (rec.get("Key") or "").strip()
        nm = (rec.get("Name") or "").strip()
        if k and _eligible(k, nm):
            _by_identity.setdefault(variety_identity(k, nm), []).append(k)
    dup_losers = {k for keys in _by_identity.values() if len(keys) > 1
                  for k in keys if k != survivor_of(keys, _seed)}
    delete_keys = delete_keys | dup_losers
    for record in records:
        name = (record.get("Name") or "").strip()
        key = (record.get("Key") or "").strip()
        # The Id-free base is the matcher's fallback source when the live export is absent (post factory
        # reset -- see load_all). It has no Id column, so use the Key as the variation_id: Keys are unique,
        # so every base variety indexes distinctly instead of colliding on an empty id, and the matcher
        # recognizes it rather than minting a duplicate. Products link by Key (match_variation._key_for);
        # the real Medusa id fills in on the next pull. The live export (with an Id) is unaffected.
        vid = (record.get("Id") or "").strip() or key
        if not vid or not name:
            continue
        # Never match products to a JUNK existing variant -- a bare-code one ('Mgt','Gs', a supplier
        # code/brand abbreviation wrongly minted) OR a re-key OLD SIDE (dup_losers above). A variety's
        # stone TYPE is authoritative (assigned from the Key at review), so it is NOT re-judged against
        # the name here: a variety is matchable by its assigned type, never hidden on a name-vs-type guess.
        if looks_code_shaped(name) == "bare_code" or key in delete_keys:
            continue
        if key_prefix and not key.casefold().startswith(key_prefix.casefold()):
            continue
        aliases_raw = (record.get("Aliases") or "").strip()
        aliases = [a.strip() for a in aliases_raw.split("|") if a.strip()] if aliases_raw else []
        table.by_id[vid] = Variant(
            variation_id=vid, key=key, name=name,
            image=(record.get("Image") or "").strip(), aliases=aliases,
        )
        # NOTE: surface_to_id is intentionally NOT populated here -- production matching builds its own
        # exact index in matching/index.py; this per-row normalize+insert over ~24k variants was pure
        # wasted work (the field stays for the test that clears it).
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

    def apply_leaf_overlay(self, overlay: dict[tuple[str, str], dict[str, list[str]]]) -> int:
        """Grow varieties' allowed sets from an operator-approved overlay, keyed by (norm name, norm
        stone_type) -> {attribute: [values]}. Additive + idempotent: a value already allowed is skipped.
        The committed backbone seed is never touched -- this only widens the in-memory sets, so dropping
        the overlay restores the pristine tree. Returns how many (variety, value) pairs were added."""
        fields = {"color": "colors", "finish": "finishes", "quality": "qualities"}
        added = 0
        for varieties in self.by_norm_name.values():   # every variety lives here; aliases point to the same objects
            for variety in varieties:
                adds = overlay.get((_norm(variety.variant), _norm(variety.stone_type)))
                if not adds:
                    continue
                for attribute, values in adds.items():
                    field_name = fields.get(attribute)
                    if not field_name:
                        continue
                    target = getattr(variety, field_name)
                    have = {_norm(x) for x in target}
                    for value in values:
                        if _norm(value) not in have:
                            target.append(value)
                            have.add(_norm(value))
                            added += 1
        return added

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
    if not path.exists():
        from stone_pipeline.config.domain import active_pack
        raise FileNotFoundError(
            f"backbone file {path} is missing for domain pack {active_pack().name!r}. Each pack ships its own "
            "catalog_source/<pack>/ backbone JSON; a non-stone pack never falls back to stone's data. Add the "
            "pack's backbone file before running it.")
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
        return [pid for lst in self.by_country.values() for pid in lst]


def load_ports(path: Path | None = None) -> Ports:
    """Load the env's Medusa port ids from from_medusa/<env>/ports.csv. Port ids differ per env, so
    PRODUCTION resolves ONLY against its own downloaded file and REFUSES the dev-id catalog_source
    fallback: shipping dev ids into prod makes Medusa silently drop every port (all port_of_origin
    blank). Dev may fall back to the committed catalog_source master. An explicit `path` (tests)
    is honored verbatim, env-independent."""
    if path is not None:
        candidate = Path(path)
    else:
        candidate = SETTINGS.paths.ports_csv
        if not candidate.exists():
            if IS_PRODUCTION:
                # No prod ports export in place yet. Return empty so ports resolve to nothing and every
                # row is flagged port_unresolved (visible in review), instead of SILENTLY loading dev
                # ids. Self-heals once from_medusa/production/ports.csv is fetched. Never fall back to
                # the dev-id master in prod -- same invariant as the prod bucket/owner-id guards.
                log.error("production ports.csv missing (%s): ports UNRESOLVED until the prod Medusa "
                          "ports export is placed there -- refusing the dev-id fallback",
                          SETTINGS.paths.ports_csv)
                return Ports()
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

    def convert(self, value: float, token: str, expect: str | None = None) -> Optional[float]:
        """Scale `value` by the token's factor. When `expect` is given (e.g. "weight"), a token of a
        DIFFERENT dimension returns None instead of a silently-wrong scaling -- so a length/area token
        that leaked into a weight field is rejected rather than shipped as a plausible-magnitude kg.
        The `dimension` column carries this; without `expect` the behaviour is unchanged (dimension ignored)."""
        entry = self.by_token.get((token or "").strip().casefold())
        if entry is None:
            return None
        if expect is not None and entry.dimension != expect:
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
            # empty factor is a legit same-unit token (-> 1.0); a PRESENT but non-numeric factor is bad data
            # -- skip it loudly rather than crash the whole reference load or silently mis-scale to 1.0.
            factor_raw = (record.get("factor") or "").strip()
            factor = 1.0 if not factor_raw else parse_number(factor_raw)
            if factor is None:
                log.warning("units: skipping token with a non-numeric factor",
                            extra={"extra_fields": {"token": token, "factor": factor_raw}})
                continue
            units.by_token[token] = UnitEntry(
                dimension=(record.get("dimension") or "").strip(),
                canonical=(record.get("canonical") or "").strip(),
                factor=factor,
            )
    return units


# --- synonyms/<vocab>.csv -----------------------------------------------------
@lru_cache(maxsize=None)
def load_synonyms(vocab: str, directory: Path | None = None) -> dict[str, str]:
    """raw (normalized) -> canonical backend value, or 'none' to resolve to no id
    without an error (section 7, Stage 3). Missing file is an empty map. Cached: it is read once per
    vocab and consulted per-row (resolver + name-detection), and synonyms are static within a run."""
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
            self._index = {(_norm(r.variety), _norm(r.stone_type)): r for r in self.rules}

    def exact(self, name: str, stone_type: str | None = None) -> Optional[OriginRule]:
        """The (name, stone_type) rule, or None. STRICT: a homonym differs by type and there is no fallback,
        so a miss returns None (derive flags it) rather than a country guessed from a different type."""
        self._ensure_index()
        return self._index.get((_norm(name), _norm(stone_type or "")))

    def apply_origin_overlay(self, minted: dict[tuple[str, str], str]) -> int:
        """Overlay operator-minted origins ((name, type) -> ISO2): effective map = CSV + mints. A mint
        REPLACES the CSV rule for that SAME (name, type) only; a type-less mint is skipped (can't emit).
        Returns the count added or overridden."""
        if not minted:
            return 0
        by_key = {(_norm(r.variety), _norm(r.stone_type)): i for i, r in enumerate(self.rules)}
        added = 0
        for (variety, stone_type), iso in minted.items():
            iso = (iso or "").strip().upper()
            if not variety or not iso or not (stone_type or "").strip():
                continue                         # type-less mint can't be type-scoped -> skip (never emits)
            rule = OriginRule(variety=variety, country_iso=iso, city="", county="", stone_type=stone_type)
            idx = by_key.get((_norm(variety), _norm(stone_type)))
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
            key = (_norm(variety), _norm(st))
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
        return self.rules.get((_norm(source), _norm(variety), _norm(stone_type or "")))

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
    path = Path(path) if path else SETTINGS.paths.origin_overrides_csv
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
            ov.rules[(_norm(source), _norm(variety), _norm(st))] = iso
    if skipped:
        log.warning("origin overrides rows skipped (need source + variety + stone_type + country_iso)",
                    extra={"extra_fields": {"skipped": skipped, "path": str(path)}})
    return ov


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
    origin_overrides: OriginOverrides = field(default_factory=OriginOverrides)  # per-supplier origin picks
    overrides: object = None  # state.overrides.Overrides; lazy import to avoid cycle
    # norm(cleaned variety name) -> operator-chosen canonical stone type, for MINT decisions (config.db).
    # The matcher consults this so a product whose scraped type matches no existing variety of its name binds
    # to the operator-minted (name, type) instead of gapping -- the operator's authority reaching the PRODUCT,
    # not just the variety. Folded in at load (below), mirroring the seed_country origin overlay.
    variety_seed_types: dict[str, str] = field(default_factory=dict)

    @cached_property
    def valid_iso_codes(self) -> frozenset:
        """The real ISO-3166 alpha-2 codes we know (the values of country_codes), so a bare 2-letter
        scraped token can be VALIDATED rather than blindly trusted (rejects 'XX'; lets 'UK' resolve
        to GB via the name/alias path instead of passing through as a bogus code)."""
        return frozenset(self.country_codes.values())

    # A specific category's variant table is `self.variants[<category name>]` (keyed by the active pack's
    # category names) -- no per-product-type accessor, so nothing here assumes a 'slab'/'block'/'tile' pack.


def _assert_pack_defaults_resolve(ref: ReferenceData) -> None:
    """Fail LOUD if a domain-pack default VALUE (or a colour classify() can emit) is not a real Medusa value
    in attributes.csv. Field NAMES are the Medusa contract; the VALUES must exist in Medusa's vocabulary --
    so a pack/hardcoded default that Medusa renamed or removed is caught here at load, not shipped downstream
    as an unresolvable null id. Reuses the same resolve_id every stage uses."""
    from stone_pipeline.config.domain import active_pack
    from stone_pipeline.stages.variety_color import CLASSIFIABLE_COLORS
    pack = active_pack()
    checks: list[tuple[str, str]] = (
        [("finish", f) for f in pack.default_finishes]
        + [("finish", f) for f in pack.last_resort_finishes.values()]
        + [("finish", pack.block_finish), ("quality", pack.last_resort_quality),
           ("color", pack.fallback_color)])
    # the classify() palette only needs to resolve in Medusa if the domain actually classifies texture colour
    if pack.classify_texture_color:
        checks += [("color", c) for c in CLASSIFIABLE_COLORS]
    # every SYNONYM's canonical target must also be a real Medusa value: a synonym 'Grigio'->'Gray' while
    # attributes.csv has only 'Grey' otherwise resolves to a null id PER PRODUCT (a quiet per-row reject
    # for every product routing through it) instead of one loud config error here at load. Skip the 'none'
    # sentinel (a deliberate resolve-to-no-id). Symmetric with the pack-default checks above.
    for vocab in pack.attributes:
        checks += [(vocab, c) for c in load_synonyms(vocab).values()
                   if c and c.strip().casefold() != "none"]
    missing = sorted({(v, val) for v, val in checks if val and not ref.attributes.resolve_id(v, val)})
    if missing:
        raise ValueError("pack default OR synonym-target values absent from attributes.csv (a value Medusa "
                         "does not have): " + ", ".join(f"{v}={val!r}" for v, val in missing))


def load_all() -> ReferenceData:
    from stone_pipeline.state.overrides import load_overrides
    from stone_pipeline.config import decisions_store, settings, store

    # load_all is a READ path. The operator overlays below live in config.db, but merely READING them via
    # open_store() CREATES the file if it is absent -- which then flips load_source (config store vs yaml) for
    # the rest of the process and is wrong for a pure read. Guard every decisions_store read on the store
    # already existing: in prod it always does (control plane, snapshotted + restored on boot), so the overlays
    # always apply; when it is absent (tests, a fresh checkout) there are no decisions to overlay anyway, so
    # skipping is equivalent -- minus the surprise file. See the origin-map overlay note below.
    _have_config_db = store.config_db_path().exists()

    paths = SETTINGS.paths
    # Realign the category registry with the export NOW on disk, BEFORE any stage reads category
    # activation. The pcats are read once at import (bootstrap), which for a run whose export landed after
    # import (a cold start, or any non-produce entrypoint like catalog/build/republish that never fetched)
    # leaves block/tile inactive on the frozen snapshot -- silently disabling new-variety fan-out into
    # those categories. load_all is the single point every pipeline stage passes through with the export on
    # disk, so refreshing here makes EVERY path (not just produce) reflect the live pcats. Idempotent.
    settings.refresh_category_pcats()
    # The effective backbone = the committed seed grown by the operator-approved leaf overlay (config.db).
    # The seed file is never mutated; the overlay is applied in memory, here, in the one place ref is built.
    backbone = load_backbone()
    if _have_config_db:
        backbone.apply_leaf_overlay(decisions_store.backbone_leaf_overlay())
    # The matcher's existing-variety index reads the live Medusa export (paths.export_file). A factory reset
    # DELETES that file (lifecycle._prune_stale_medusa_export) and Blokport re-exports it only after the next
    # pull -- so between those the export is absent. Fall back to the committed Id-free BASE (the full variety
    # union) so the matcher still recognizes every existing variety instead of mass-minting them all as new
    # (products link by Key, not id; the real Medusa id fills in on the pull). SCOPED to the matcher on
    # purpose: emit / tree_build / curate keep reading paths.export_file, so they never treat the id-free base
    # as the live id-bearing export.
    matcher_export = paths.export_file if paths.export_file.exists() else paths.variants_export_base_csv
    ref = ReferenceData(
        attributes=load_attributes(),
        # ONE combined export for every category; the category is the Key prefix, so
        # the same file is split into a per-category index, one per registry entry
        # that shares the stone-variety vocabulary.
        variants={c.name: load_variants(matcher_export, c.name, key_prefix=c.name)
                  for c in CATEGORIES if c.shares_variety_vocab},
        backbone=backbone,
        ports=load_ports(),
        units=load_units(),
        origin_map=load_origin_map(),
        origin_overrides=load_origin_overrides(),
        country_codes=load_country_codes(),
        synonyms={v: load_synonyms(v) for v in VOCAB_CATEGORIES},
        overrides=load_overrides(),
        versions={
            "attributes": content_hash(paths.attributes_csv),
            "variants_export": content_hash(paths.export_file),
            "backbone": content_hash(paths.backbone_json),
            "ports": content_hash(
                paths.ports_csv if paths.ports_csv.exists()
                # prod never provenances the dev-id fallback (load_ports refuses it): hash the absent
                # primary ("absent") so the version reflects what actually resolved.
                else (paths.ports_csv if IS_PRODUCTION else paths.ports_csv_fallback)
            ),
            "units": content_hash(paths.units_csv),
            "origin_map": content_hash(
                paths.origin_map_csv if paths.origin_map_csv.exists() else paths.origin_map_csv_fallback
            ),
        },
    )
    # The effective origin map = the curated CSV grown by operator-minted origins (variety_decision
    # seed_type + seed_country), overlaid in memory here -- the one place ref is built, mirroring the leaf
    # overlay. Type-scoped, so a mint under one type never clobbers a homonym's origin under another.
    # Operator overlays from config.db (guarded on the store already existing -- see the note in load_all):
    #   * seed_country grows the per-variety origin MAP (type-scoped, so a mint under one type never clobbers
    #     a homonym's origin under another);
    #   * origin CONFIRMATIONS from the separate origin review queue grow the per-vendor origin OVERRIDES, so a
    #     confirmed (source, variety, type) origin resolves at derive's supplier_override tier and is never
    #     re-asked;
    #   * operator MINT types (canonical-gated, keyed by norm(clean variety name) exactly as decisions_store +
    #     curate key them) let the matcher bind a product to an operator-minted (name, type) whose type the
    #     scrape did not carry -- the mint decision reaching the product, not only the variety.
    ref.variety_seed_types = {}
    if _have_config_db:
        ref.origin_map.apply_origin_overlay(decisions_store.variety_seed_country_rules())
        # Operator "edit origins" edits win over both the CSV base and a mint's seed_country: applied LAST,
        # they set the variety's origin country LIST (the per-vendor gate then picks from it). Same overlay
        # machinery as the mint origin above, keyed by (name, type), country_iso may be a comma-list.
        ref.origin_map.apply_origin_overlay(decisions_store.variety_origins())
        ref.origin_overrides.apply_overlay(decisions_store.origin_decisions())
        _valid_types = {_norm(t) for t in ref.attributes.canonical_names("type")}
        ref.variety_seed_types = {n: t for n, t in decisions_store.variety_seed_types().items()
                                  if _norm(t) in _valid_types}
    _assert_pack_defaults_resolve(ref)   # a pack default value not in Medusa's vocabulary fails loud here
    log.info(
        "reference loaded",
        extra={
            "extra_fields": {
                "attributes_colors": len(ref.attributes.by_category.get("color", {})),
                # the default-form variant count (pack primary category); NOT literally slab, so a non-stone
                # pack whose default form is not named 'slab' does not KeyError building this log line.
                "variants_default_form": (
                    len(ref.variants[settings.default_form_name()].by_id)
                    if settings.default_form_name() in ref.variants else 0),
                "backbone_varieties": len(ref.backbone),
            }
        },
    )
    return ref
