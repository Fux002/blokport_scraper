"""The existing-variety vocabulary, for the review alias flow.

ONE place that answers "what varieties exist?" -- used by GET /config/v1/varieties (the alias-to dropdown)
and by the alias-decision validator. Its source is the LEDGER `variation` table: the durable, scraper-owned
record of every variety, which SURVIVES a reset (reset re-arms variation state to pending but never deletes
the rows) and is always consistent with the variation lane. It is deliberately NOT the fetched Medusa export
(variants_export.csv) -- that file is best-effort and reflects Medusa's CURRENT state, so it is empty in the
window right after a reset (products cleared) and before the re-pull, which would leave the alias flow
unusable exactly when a fresh scrape needs it.

The "an accepted alias is one curate can attach" invariant still holds: curate attaches at PRODUCE time
against the freshly-fetched export (Medusa is repopulated by then) and alias decisions apply on the next
run -- so the durable ledger drives OFFER + VALIDATE, the fresh export drives ATTACH. The picker and the
validator now agree by construction (same source), which the export-backed version did not guarantee once
the file went stale.

If no produce has ever run on this host (no ledger yet), the vocab is empty and every alias is refused --
a safe, explicit failure, not a fallback that accepts an alias that would resolve to nothing.

Read per call (admin traffic is low-QPS); no caching layer, to keep this simple and never stale.
"""

from __future__ import annotations

from stone_pipeline.matching import projections as proj


def _like_escape(s: str) -> str:
    """Make LIKE wildcards literal so a search term with % or _ is not a wildcard."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rows(q: str | None = None) -> list[dict]:
    """Existing, non-retired varieties from the ledger variation table: [{'name', 'stone_type'}], deduped
    by normalized name (a variety shared across branches is ONE alias target) and sorted by name. Optional
    `q` filters by name substring in SQL, so a type-ahead never materialises the full ~25k set. Empty (no
    side effect) when no ledger exists yet."""
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    from stone_pipeline.stages import decisions
    if not writethrough.ledger_path().exists():
        return []                                     # no produce yet -> empty, every alias refused
    retired = decisions.load_retired()
    sql = "SELECT key, name, type FROM variation WHERE name IS NOT NULL AND name != ''"
    params: tuple = ()
    if q:
        sql += " AND name LIKE ? ESCAPE '\\'"
        params = (f"%{_like_escape(q)}%",)
    sql += " ORDER BY name COLLATE NOCASE"
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        for r in lg.execute(sql, params):
            if r["key"] in retired:                   # retired varieties are not valid alias targets
                continue
            # Dedup on (name, TYPE), not name alone: the slab/block/tile copies of ONE variety share both
            # and collapse to a single alias target, but a legitimately multi-type name ('Coffee' = Marble
            # AND Onyx) keeps BOTH as distinct targets -- else one stone is hidden from the alias dropdown.
            key = (proj.norm(r["name"]), proj.norm(r["type"] or ""))
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": r["name"], "stone_type": r["type"] or ""})
    return out


def list_all(q: str | None = None, limit: int | None = None) -> list[dict]:
    """Existing variety names (+ stone_type) for the alias-to dropdown, sorted. `q` filters by name
    substring (for a type-ahead); `limit` caps the result (the full set is ~25k)."""
    rows = _rows(q)
    return rows[:limit] if limit and limit > 0 else rows


def exists(name: str) -> bool:
    """True iff `name` is an existing variety -- the set an alias can resolve onto. Same ledger source as
    the dropdown, so the picker and the validator agree by construction."""
    n = proj.norm(name or "")
    return bool(n) and any(proj.norm(v["name"]) == n for v in _rows())


def exists_as(name: str, stone_type: str) -> bool:
    """True iff `name` already exists as a variety OF `stone_type`. Identity is (type, name), so a mint's
    operator-corrected name (mint + rename) is refused only when that exact pair exists -- 'Calacatta' as a
    Quartzite is legal beside the Marble one. Same ledger source as exists()."""
    n, t = proj.norm(name or ""), proj.norm(stone_type or "")
    return bool(n) and bool(t) and any(
        proj.norm(v["name"]) == n and proj.norm(v["stone_type"]) == t for v in _rows())


def alias_target(name: str, stone_type: str) -> str | None:
    """Canonical variety NAME when `name` is an existing ALIAS (exact, normalized) of a variety OF
    `stone_type`, else None. A statement naming a known alias must bind to the variety that alias already
    resolves to, never mint a duplicate of it (e.g. 'Artemis' is an alias of 'Andes' Quartzite, so a mint of
    'Artemis' Quartzite is really a bind to 'Andes'). Match is normalized equality ONLY -- never fuzzy or
    phonetic -- and type-scoped, so the atomic (type, name) identity is preserved. Ambiguous (the alias sits
    on two distinct same-type varieties) -> None, never a guess. Same ledger source as exists_as; None when
    no ledger exists yet."""
    import json

    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    from stone_pipeline.stages import decisions
    n, t = proj.norm(name or ""), proj.norm(stone_type or "")
    if not n or not t or not writethrough.ledger_path().exists():
        return None
    retired = decisions.load_retired()
    hits: set[str] = set()
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        for r in lg.execute("SELECT key, name, type, aliases FROM variation "
                            "WHERE name IS NOT NULL AND name != '' AND aliases IS NOT NULL AND aliases != ''"):
            if r["key"] in retired or proj.norm(r["type"] or "") != t:
                continue
            try:
                aliases = json.loads(r["aliases"])
            except (ValueError, TypeError):
                continue
            if any(proj.norm(a) == n for a in aliases):
                hits.add(r["name"])
    return next(iter(hits)) if len(hits) == 1 else None    # never guess across an ambiguous alias
