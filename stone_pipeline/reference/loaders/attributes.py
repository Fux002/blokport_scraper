"""attributes.csv: attribute names -> backend ids (colour, finish, quality, type) and the category pcat ids."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from stone_pipeline.config.domain import active_pack
from stone_pipeline.reference.loaders.common import env, norm

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


def load_attributes(path: Path | None = None) -> Attributes:
    path = Path(path or env().SETTINGS.paths.attributes_csv)
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
                nk = norm(value)
                # Fail loud on a CONFLICTING duplicate: two names in a category that collapse to the same
                # normalized key but carry DIFFERENT Medusa ids. The value->id map silently last-wins
                # otherwise, so one id would shadow the other (and norm folds accents/separators, so two
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
