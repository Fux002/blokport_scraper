"""Legacy-shaped views over the one decisions object, for assertions only. The store used to expose the
mint/reject/binding/origin maps as separate accessors; tests that asserted on those shapes read the same
facts from `load_decisions()`. Every view takes the lookups a test injected into `decide()` (`exists_as`,
`alias_target`), so the derivation matches what the statement meant when it was made."""

from __future__ import annotations

from stone_pipeline.config import decisions_store
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.matching import projections as proj


def _norm(s: str | None) -> str:
    return proj.norm(s or "")


def load(**lookups) -> Decisions:
    return decisions_store.load_decisions(**lookups)


def mint(spelling: str, stone_type: str | None = None, name: str | None = None, color: str | None = None,
         origin: str | None = None, source: str = "", asked_by: str = "") -> None:
    """A stored 'is' statement in the legacy mint shape (the seeds, at `source`'s level, '' = every vendor),
    without the lookups decide() needs. A rename to the same spelling is no rename."""
    if name and _norm(name) == _norm(spelling):
        name = None
    decisions_store._upsert_statement(source, spelling, "is", name=name, stone_type=stone_type, color=color,
                                      origin_iso=origin, asked_by=asked_by or source)


def actions(**lookups) -> dict[tuple[str, str], dict]:
    """variety_actions(): {(vendor, spelling): {action, seed_*, spelling, source, asked_by}} (mint + reject)."""
    return {k: {"action": "mint" if s.is_mint else "reject", "seed_color": s.color, "seed_type": s.stone_type,
                "seed_country": s.origin_iso, "seed_name": s.name, "spelling": s.display, "source": s.source,
                "asked_by": s.asked_by}
            for k, s in load(**lookups).statements.items()}


def confirm(**lookups) -> dict[tuple[str, str], str]:
    return {k: ("yes" if s.is_mint else "no") for k, s in load(**lookups).statements.items()}


def rejected(**lookups) -> set[tuple[str, str]]:
    return {k for k, s in load(**lookups).statements.items() if not s.is_mint}


def seed_colors(**lookups) -> dict[tuple[str, str], str]:
    return {k: s.color for k, s in load(**lookups).statements.items() if s.is_mint and s.color}


def seed_names(**lookups) -> dict[tuple[str, str], str]:
    return {k: s.name for k, s in load(**lookups).statements.items() if s.is_mint and s.name}


def seed_types(**lookups) -> dict[tuple[str, str], str]:
    return load(**lookups).seed_types()


def seed_scopes(**lookups) -> dict[tuple[str, str], str]:
    return {k: s.source for k, s in load(**lookups).statements.items() if s.is_mint and s.source}


def country_rules(**lookups) -> dict[tuple[str, str], str]:
    return load(**lookups).mint_origin_rules()


def countries(**lookups) -> dict[str, str]:
    return {(_norm(s.name) if s.name else s.spelling): s.origin_iso
            for s in load(**lookups).statements.values() if s.is_mint and s.origin_iso}


def scoped(**lookups) -> dict[tuple[str, str], tuple[str, str]]:
    return dict(load(**lookups).bindings)


def origins(**lookups) -> dict[tuple[str, str, str], str]:
    return dict(load(**lookups).vendor_origins)


def widen(**lookups) -> dict[tuple[str, str, str], str]:
    return dict(load(**lookups).widened)
