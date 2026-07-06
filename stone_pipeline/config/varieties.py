"""The existing-variety vocabulary, for the review alias flow.

ONE place that answers "what varieties exist?" -- used by GET /config/v1/varieties (the alias-to dropdown)
and by the alias-decision validator. Its source is `curate.load_existing` (the Medusa export), which is
EXACTLY the surface curate attaches an alias onto. Deriving the vocab from the same parser is deliberate:
an alias the API accepts is therefore always one curate can attach (never a silent dead-end), and there
is no second CSV parse to drift. Retired varieties are excluded (they are not valid resolution targets).

If the export is not present yet (no produce has run on this host), the vocab is empty and every alias is
refused -- a safe, explicit failure, not a fallback that accepts an alias that would resolve to nothing.

Loaded per call (admin traffic is low-QPS); no caching layer, to keep this simple and never stale.
"""

from __future__ import annotations

from stone_pipeline.matching import projections as proj


def _stone_type_from_key(key: str) -> str:
    """The variety Key is `{branch}_{type}_{name}_{uuid}`; the 2nd token is the stone-type slug."""
    parts = (key or "").split("_")
    return parts[1].replace("-", " ").title() if len(parts) >= 2 and parts[1] else ""


def _canonical() -> dict[str, dict]:
    """norm(canonical name) -> {'name', 'stone_type'} for every existing, non-retired variety. Keyed the
    SAME way curate keys `existing_surface`, so validation == the dropdown == what curate can attach to."""
    from stone_pipeline.stages import curate, decisions
    retired = decisions.load_retired()
    out: dict[str, dict] = {}
    for branch in curate.active_branches():
        for nrm, v in curate.load_existing(branch).by_name.items():
            if v.get("Key") in retired:
                continue
            out.setdefault(nrm, {"name": v["Name"], "stone_type": _stone_type_from_key(v.get("Key", ""))})
    return out


def list_all() -> list[dict]:
    """Every existing variety name (+ stone_type), sorted, for the alias-to dropdown."""
    return sorted(_canonical().values(), key=lambda d: d["name"].casefold())


def exists(name: str) -> bool:
    """True iff `name` is an existing CANONICAL variety -- the exact set curate can attach an alias onto."""
    return proj.norm(name or "") in _canonical()
