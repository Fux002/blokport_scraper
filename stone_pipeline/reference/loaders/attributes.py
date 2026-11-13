"""attributes.csv: attribute names -> backend ids (colour, finish, quality, type). A category's pcat id is NOT read here:
the category registry (config/categories.category_pcats) is the one reader of the category rows (table 577)."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import ATTRIBUTES_STATUS_REQUIRED
from stone_pipeline.reference.loaders.common import env, norm
from stone_pipeline.reference.medusa_files import CATEGORY_KIND, AttributeValue, read_attributes

# Vocabulary categories carried in attributes.csv (section 6), declared by the active product-domain pack
# (the stone pack reproduces the historical set). Used only to load the per-attribute synonyms, so order
# is immaterial.
VOCAB_CATEGORIES = tuple(active_pack().attributes)

# The seed's types for the one backbone file version they were read from (_seed_types), replaced when the file changes;
# the lock keeps the config server's request threads from reading it half-replaced.
_SEED_TYPES: dict[tuple[str, int, int], frozenset[str]] = {}
_SEED_TYPES_LOCK = threading.Lock()


@dataclass
class Attributes:
    """Name to backend id, per vocabulary."""

    # category -> {normalized_name -> (canonical_name, id)}
    by_category: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)

    def resolve_id(self, category: str, name: str) -> Optional[tuple[str, str]]:
        """Return (canonical_name, id) for an exact normalized name, else None. For types, a synonym
        ('Sodalite' -> 'Sodalite Syenite') falls back to the canonical attribute so a commercial/short
        name resolves without polluting the type vocabulary itself."""
        table = self.by_category.get(category, {})
        hit = table.get(norm(name))
        if hit is not None:
            return hit
        if category == "type":
            canonical = env().load_synonyms("type").get(norm(name))
            if canonical:
                return table.get(norm(canonical))
        return None

    def canonical_names(self, category: str) -> list[str]:
        return [canon for canon, _ in self.by_category.get(category, {}).values()]

    def all_ids(self) -> list[str]:
        return [i for table in self.by_category.values() for _, i in table.values()]


def restricted_values(path: Path | None = None) -> dict[str, set[str]]:
    """{category: {norm(value), ...}} for every RESTRICTED value in attributes.csv: archived OR internal
    (`not AttributeValue.assignable`). Read through medusa_files.read_attributes, the single authority on
    what is restricted (VE2 ruling: no ledger copy). Empty while attributes.csv carries no status/internal
    columns (the expand phase, so nothing is restricted yet) or the file is absent. Values are keyed by
    `norm` so a caller matches a resolved value name regardless of separator/case/accent; a category is keyed by
    its exact handle (table 577), the identity every category reader uses."""
    path = Path(path) if path is not None else env().SETTINGS.paths.attributes_csv
    if not Path(path).exists():
        return {}
    out: dict[str, set[str]] = defaultdict(set)
    for a in read_attributes(Path(path), require_status=ATTRIBUTES_STATUS_REQUIRED):
        if not a.assignable:                      # a category by its exact handle, never its label (577)
            out[a.category].add(a.handle if a.category == CATEGORY_KIND else norm(a.value))
    return dict(out)


def restricted_ids(path: Path | None = None) -> set[str]:
    """The Medusa ids of every RESTRICTED value (archived OR internal) in attributes.csv, for the operator's
    attribute-paste gate (a paste of a restricted id is refused). Same source (medusa_files.read_attributes)
    and same dormancy as restricted_values: empty while the file carries no status/internal columns or is
    absent, so the gate is a no-op in the expand phase."""
    path = Path(path) if path is not None else env().SETTINGS.paths.attributes_csv
    if not Path(path).exists():
        return set()
    return {a.sourceid for a in read_attributes(Path(path), require_status=ATTRIBUTES_STATUS_REQUIRED)
            if a.sourceid and not a.assignable}


def attributes_status_required(path: Path) -> bool:
    """Whether reading `path` requires the status/internal columns: only Medusa's live attributes.csv download
    carries them. An internal temp dump of the ledger (ledger.render) is the old shape and is read tolerant,
    so the decision is a pure function of the path and step C's flag flip touches only the real download. The
    variants export has the mirror helper variants_status_required."""
    return ATTRIBUTES_STATUS_REQUIRED and Path(path) == env().SETTINGS.paths.attributes_csv


def _pack_handles() -> frozenset[str]:
    return frozenset(c["plural"] for c in active_pack().categories)   # D11: a category's handle is the pack's plural


def _seed_types() -> frozenset[str]:
    """norm of every type a variety of the pack's committed seed carries (its backbone, read live through the one
    backbone reader). The pack's yaml decides: a pack naming no backbone_file (lime) holds no seed variety, so rule (ii)
    adds no type and its types are its combinations' alone; a pack that names one must ship it, and an absent file
    raises FileNotFoundError naming it (never read as "no seed types", which would quietly shrink the vocabulary).
    Parsed once per file version (path, size, mtime), as C3's seed cache does: the config server loads the vocabulary
    per request, and the backbone is 12,156 posts."""
    from stone_pipeline.reference.loaders import backbone
    if active_pack().backbone_file is None:
        return frozenset()
    path = Path(env().SETTINGS.paths.backbone_json)
    stat = path.stat()
    version = (str(path), stat.st_size, stat.st_mtime_ns)
    with _SEED_TYPES_LOCK:
        if version not in _SEED_TYPES:
            _SEED_TYPES.clear()
            _SEED_TYPES[version] = frozenset(norm(p["stone_type"]) for p in backbone.read_posts(path)
                                             if p.get("stone_type"))
        return _SEED_TYPES[version]


def _in_pack(value: AttributeValue, pack_handles: frozenset[str], seed_types: frozenset[str]) -> bool:
    """A type is the PACK's iff (i) one of its valid combinations is in a pack category, OR (ii) the committed seed holds
    a variety of that type (table 569, Mas's ruling 12 Nov; both read live, no type list anywhere). Anything else
    (BlokPort's Steel or Wine, a type with no combination and no seed variety) is never guessed in. A seed type that BE
    lists with ONLY non-pack handles raises, naming the type: the seed and BE disagree, and neither side wins quietly."""
    handles = value.category_handles or ()
    by_handle = not pack_handles.isdisjoint(handles)
    in_seed = norm(value.value) in seed_types
    if in_seed and handles and not by_handle:
        raise ValueError(f"type {value.value!r}: the committed seed holds varieties of it, but BlokPort lists it only in "
                         f"non-pack categories ({'|'.join(handles)}); the seed and BE disagree (table 569)")
    return by_handle or in_seed


def types_outside_pack(path: Path | None = None) -> list[dict]:
    """Every type load_attributes leaves out, sorted by name, with its handles and why (`no_combination` or
    `other_categories`): the run lists them (catalog.write_types_outside_pack), so an exclusion is never silent."""
    pack_handles, seed_types = _pack_handles(), _seed_types()
    path = Path(path or env().SETTINGS.paths.attributes_csv)
    return sorted(({"type": a.value, "category_handles": "|".join(a.category_handles or ()),
                    "reason": "other_categories" if a.category_handles else "no_combination"}
                   for a in read_attributes(path, require_status=ATTRIBUTES_STATUS_REQUIRED,
                                            require_category_handles=True)
                   if a.category == "type" and not _in_pack(a, pack_handles, seed_types)), key=lambda t: t["type"])


def load_attributes(path: Path | None = None) -> Attributes:
    """The name->id tables of every value that has an id (an id-less value cannot be emitted), the type table
    holding only the PACK's types. This is the one place the type vocabulary is scoped (table 569), so the type
    words, the vocabulary words and the type resolution all read the same set; types_outside_pack lists the rest.
    An attributes.csv without the category_handles column is refused: without it no type can be placed in the pack.
    read_attributes guards conflicting duplicates."""
    path = Path(path or env().SETTINGS.paths.attributes_csv)
    pack_handles, seed_types = _pack_handles(), _seed_types()
    attrs = Attributes()
    for a in read_attributes(path, require_status=ATTRIBUTES_STATUS_REQUIRED, require_category_handles=True):
        if not a.sourceid or (a.category == "type" and not _in_pack(a, pack_handles, seed_types)):
            continue
        if a.category != CATEGORY_KIND:            # a category is the registry's (keyed by its handle)
            attrs.by_category.setdefault(a.category, {})[norm(a.value)] = (a.value, a.sourceid)
    return attrs
