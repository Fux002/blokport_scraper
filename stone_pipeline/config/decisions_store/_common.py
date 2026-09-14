"""Shared helpers for the decisions_store package: the timestamp, the one normalizer, and the one error the
writers raise. Every submodule imports from here so there is a single definition of each."""

from __future__ import annotations

from datetime import datetime, timezone

from stone_pipeline.matching import projections as proj


class InvalidDecision(ValueError):
    """A decision payload the store refuses (missing required field, unknown kind/action, bad ISO)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(s: str) -> str:
    return proj.norm(s or "")
