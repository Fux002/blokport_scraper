"""attributes.csv: attribute names -> backend ids (colour, finish, quality, type) and the category pcat ids."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from stone_pipeline.config.domain import active_pack
from stone_pipeline.config.settings import ATTRIBUTES_STATUS_REQUIRED
from stone_pipeline.reference.loaders.common import env, norm
from stone_pipeline.reference.medusa_files import read_attributes

# Vocabulary categories carried in attributes.csv (section 6), declared by the active product-domain pack
# (the stone pack reproduces the historical set). Used only to load the per-attribute synonyms, so order
# is immaterial.
VOCAB_CATEGORIES = tuple(active_pack().attributes)


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
        return list(self.category_pcat.values()) + [
            i for table in self.by_category.values() for _, i in table.values()]


def restricted_values(path: Path | None = None) -> dict[str, set[str]]:
    """{category: {norm(value), ...}} for every RESTRICTED value in attributes.csv: archived OR internal
    (`not AttributeValue.assignable`). Read through medusa_files.read_attributes, the single authority on
    what is restricted (VE2 ruling: no ledger copy). Empty while attributes.csv carries no status/internal
    columns (the expand phase, so nothing is restricted yet) or the file is absent. Values are keyed by
    `norm` so a caller matches a resolved value name regardless of separator/case/accent."""
    path = Path(path) if path is not None else env().SETTINGS.paths.attributes_csv
    if not Path(path).exists():
        return {}
    out: dict[str, set[str]] = defaultdict(set)
    for a in read_attributes(Path(path), require_status=ATTRIBUTES_STATUS_REQUIRED):
        if not a.assignable:
            out[a.category].add(norm(a.value))
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


def load_attributes(path: Path | None = None) -> Attributes:
    """The name->id tables of every value that has an id (an id-less value cannot be emitted).
    read_attributes guards conflicting duplicates."""
    attrs = Attributes()
    for a in read_attributes(Path(path or env().SETTINGS.paths.attributes_csv),
                             require_status=ATTRIBUTES_STATUS_REQUIRED):
        if not a.sourceid:
            continue
        if a.category == "category":
            attrs.category_pcat[a.value] = a.sourceid
        else:
            attrs.by_category.setdefault(a.category, {})[norm(a.value)] = (a.value, a.sourceid)
    return attrs
