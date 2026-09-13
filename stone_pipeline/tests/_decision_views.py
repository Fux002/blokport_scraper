"""Legacy-shaped views over the one decisions object, for assertions only: the store's mint/reject/seed maps
used to be separate accessors; tests that asserted on them read the same facts from `load_decisions()`."""

from __future__ import annotations

from stone_pipeline.stages import decisions


def confirm() -> dict[tuple[str, str], str]:
    return {k: ("yes" if s.is_mint else "no") for k, s in decisions.load_decisions().statements.items()}


def rejected() -> set[tuple[str, str]]:
    return {k for k, s in decisions.load_decisions().statements.items() if not s.is_mint}


def seed_colors() -> dict[tuple[str, str], str]:
    return {k: s.color for k, s in decisions.load_decisions().statements.items() if s.is_mint and s.color}


def seed_names() -> dict[tuple[str, str], str]:
    return {k: s.name for k, s in decisions.load_decisions().statements.items() if s.is_mint and s.name}
