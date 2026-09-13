"""backbone.json: per variety, the stone_type and the allowed colour, finish and quality sets (names, no ids)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from stone_pipeline.reference.loaders.common import env, norm


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
        key = norm(name)
        return self.by_norm_name.get(key) or self.by_norm_alias.get(key) or []

    def lookup(self, name: str, stone_type: str | None = None) -> Optional[BackboneVariety]:
        candidates = self.lookup_all(name)
        if not candidates:
            return None
        if stone_type:
            for variety in candidates:
                if norm(variety.stone_type) == norm(stone_type):
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
                adds = overlay.get((norm(variety.variant), norm(variety.stone_type)))
                if not adds:
                    continue
                for attribute, values in adds.items():
                    field_name = fields.get(attribute)
                    if not field_name:
                        continue
                    target = getattr(variety, field_name)
                    have = {norm(x) for x in target}
                    for value in values:
                        if norm(value) not in have:
                            target.append(value)
                            have.add(norm(value))
                            added += 1
        return added

    def is_valid_leaf(self, variety: BackboneVariety, color: str, finish: str, quality: str) -> bool:
        """Set-membership validity (section 6A): a PRESENT colour, finish, or quality must each be in
        the variety's allowed set (an empty value passes). Names compared normalized so case never
        causes a false gap."""
        cset = {norm(c) for c in variety.colors}
        fset = {norm(f) for f in variety.finishes}
        qset = {norm(q) for q in variety.qualities}
        return (
            (not color or norm(color) in cset)
            and (not finish or norm(finish) in fset)
            and (not quality or norm(quality) in qset)
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
    path = Path(path or env().SETTINGS.paths.backbone_json)
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
        backbone.by_norm_name.setdefault(norm(variant_name), []).append(variety)
        for alias in variety.aliases:
            backbone.by_norm_alias.setdefault(norm(alias), []).append(variety)
    return backbone
