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

The ledger scan is cached behind a fingerprint of the ledger (+ its WAL) and config.db, so the many name
lookups of one load_decisions do ONE scan, not one per name (the O(names x ledger) hang otherwise). The
fingerprint keeps it from ever serving stale data: any produce (ledger) or decision/reset (config.db)
changes it and the next read rebuilds.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from stone_pipeline.matching import projections as proj

_CACHE_LOCK = threading.Lock()
_CACHE: dict = {"fp": object(), "rows": [], "alias": {}}   # rows: deduped varieties; alias: (na, nt) -> name|None


def _like_escape(s: str) -> str:
    """Make LIKE wildcards literal so a search term with % or _ is not a wildcard."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fingerprint():
    """A cheap fingerprint of everything the index depends on: the ledger db + its WAL (the variety rows) and
    config.db (the retired set). Any write changes a size/mtime, so the cache never serves stale data."""
    from stone_pipeline.config import store
    from stone_pipeline.ledger import writethrough

    def _stat(p) -> tuple | None:
        try:
            s = Path(p).stat()
            return (s.st_size, s.st_mtime_ns)
        except OSError:
            return None
    lp = str(writethrough.ledger_path())
    return (_stat(lp), _stat(lp + "-wal"), _stat(store.config_db_path()))


def _index() -> tuple[list[dict], dict]:
    """(deduped variety rows, alias map) from ONE ledger scan, cached until the fingerprint changes. rows are
    [{'name','stone_type'}] deduped by (norm name, norm type); alias maps (norm alias, norm type) -> canonical
    NAME, or None where an alias sits on two distinct same-type varieties (ambiguous, never guessed). Empty
    when no ledger exists yet."""
    fp = _fingerprint()
    with _CACHE_LOCK:
        if _CACHE["fp"] == fp:
            return _CACHE["rows"], _CACHE["alias"]
    rows, alias = _build_index()
    with _CACHE_LOCK:
        _CACHE.update(fp=fp, rows=rows, alias=alias)
    return rows, alias


def _build_index() -> tuple[list[dict], dict]:
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    from stone_pipeline.stages import decisions
    if not writethrough.ledger_path().exists():
        return [], {}                                 # no produce yet -> empty, every alias refused
    retired = decisions.load_retired()
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    alias_hits: dict[tuple[str, str], set[str]] = {}
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        for r in lg.execute("SELECT key, name, type, aliases FROM variation "
                            "WHERE name IS NOT NULL AND name != '' ORDER BY name COLLATE NOCASE"):
            if r["key"] in retired:                   # retired varieties are not valid targets
                continue
            nt = proj.norm(r["type"] or "")
            # Dedup on (name, TYPE), not name alone: the slab/block/tile copies of ONE variety share both and
            # collapse to a single target, but a legitimately multi-type name ('Coffee' = Marble AND Onyx)
            # keeps BOTH as distinct targets -- else one stone is hidden from the alias dropdown.
            key = (proj.norm(r["name"]), nt)
            if key not in seen:
                seen.add(key)
                rows.append({"name": r["name"], "stone_type": r["type"] or ""})
            if r["aliases"]:
                try:
                    aliases = json.loads(r["aliases"])
                except (ValueError, TypeError):
                    aliases = []
                for a in aliases:
                    na = proj.norm(a)
                    if na:
                        alias_hits.setdefault((na, nt), set()).add(r["name"])
    alias = {k: (next(iter(v)) if len(v) == 1 else None) for k, v in alias_hits.items()}
    return rows, alias


def _rows(q: str | None = None) -> list[dict]:
    """Existing, non-retired varieties from the ledger variation table: [{'name', 'stone_type'}], deduped by
    normalized name and sorted. `q` filters by name substring (a type-ahead). Empty when no ledger exists yet.
    Backed by the cached ledger index, so calling it many times in one pass does not re-scan the ledger."""
    rows, _ = _index()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in (r["name"] or "").lower()]
    return rows


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
    on two distinct same-type varieties) -> None, never a guess. Reads the cached ledger index."""
    n, t = proj.norm(name or ""), proj.norm(stone_type or "")
    if not n or not t:
        return None
    _, alias = _index()
    return alias.get((n, t))
