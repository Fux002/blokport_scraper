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

A variety's aliases are NOT the ledger's: they are the alias stores' surfaces (aliases.surfaces), the same ones
the matcher indexes, so alias_target and the matcher agree on every known alias and code (P2-10 C3).

The ledger scan is cached behind a fingerprint of the ledger (+ its WAL), config.db, the backbone and the export,
so the many name lookups of one load_decisions do ONE scan, not one per name (the O(names x ledger) hang otherwise). The
fingerprint keeps it from ever serving stale data: any produce (ledger) or decision/reset (config.db)
changes it and the next read rebuilds.
"""

from __future__ import annotations

import threading
from pathlib import Path

from stone_pipeline.aliases.surfaces import Stores, Variety, surfaces_by_key
from stone_pipeline.matching import projections as proj

_CACHE_LOCK = threading.Lock()
# rows: deduped varieties; alias: (na, nt) -> name|None; names / pairs: the normalised name set and (name, type)
# set the lookups hit, built ONCE per scan so a lookup normalises only its own arguments (never every row).
_CACHE: dict = {"fp": object(), "rows": [], "alias": {}, "names": set(), "pairs": set(), "archived": set()}


def _fingerprint():
    """A cheap fingerprint of everything the index depends on: the ledger db (the variety rows) and config.db
    (the retired set), each as the main file's (size, mtime) plus the FRAMES in its WAL (the WAL's size).
    A write lands as WAL frames (size grows) or, at checkpoint, in the main file (mtime moves), so the
    cache never serves stale data. The WAL's mtime and existence are deliberately NOT part of it: SQLite
    deletes the WAL when the last connection closes and recreates it empty on the next open, and both
    containers open the ledger per request, so those flip constantly with no write behind them."""
    from stone_pipeline.config import store
    from stone_pipeline.ledger import writethrough

    def _main(p) -> tuple | None:
        try:
            s = Path(p).stat()
            return (s.st_size, s.st_mtime_ns)
        except OSError:
            return None

    def _frames(p) -> int:
        try:
            return Path(str(p) + "-wal").stat().st_size
        except OSError:
            return 0
    from stone_pipeline.aliases import stores as alias_stores
    from stone_pipeline.config.settings import SETTINGS
    lp, cp = writethrough.ledger_path(), store.config_db_path()
    # the alias surfaces also read the backbone (seed) and the export (the hand rows)
    return (_main(lp), _frames(lp), _main(cp), _frames(cp), _main(SETTINGS.paths.backbone_json),
            alias_stores.export_version())


def _index() -> tuple[list[dict], dict, set[str], set[tuple[str, str]], set[tuple[str, str]]]:
    """(deduped variety rows, alias map, normalised name set, normalised (name, type) set, archived pairs) from ONE ledger
    scan, cached until the fingerprint changes. rows are [{'name','stone_type'}] deduped by (norm name, norm
    type); alias maps (norm alias, norm type) -> canonical NAME, or None where an alias sits on two distinct
    same-type varieties (ambiguous, never guessed). Empty when no ledger exists yet."""
    fp = _fingerprint()
    # single-flight: the rebuild runs UNDER the lock, so concurrent requests after an invalidation wait for
    # the one build instead of each rebuilding (four polling clients turned one ~1 s build into four on a
    # 1-vCPU task, past the admin's deadline). A hit costs the lock only for the comparison.
    with _CACHE_LOCK:
        if _CACHE["fp"] != fp:
            rows, alias, names, pairs, archived = _build_index()
            _CACHE.update(fp=fp, rows=rows, alias=alias, names=names, pairs=pairs, archived=archived)
        return _CACHE["rows"], _CACHE["alias"], _CACHE["names"], _CACHE["pairs"], _CACHE["archived"]


def _index_from_rows(rows: list[dict], alias: dict, archived: set | None = None):
    """The lookup sets derived from a scan, normalised once here so exists/exists_as are set membership.
    `archived` is the (norm name, norm type) pairs whose variety is frozen (VE2)."""
    pairs = {(proj.norm(r["name"]), proj.norm(r["stone_type"] or "")) for r in rows}
    return rows, alias, {n for n, _ in pairs}, pairs, set(archived or ())


def _build_index() -> tuple[list[dict], dict, set[str], set[tuple[str, str]], set[tuple[str, str]]]:
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.stages import decisions
    if not writethrough.ledger_path().exists():
        return _index_from_rows([], {})               # no produce yet -> empty, every alias refused
    retired = decisions.load_retired()
    seen: set[tuple[str, str]] = set()
    rows: list[dict] = []
    live: list[Variety] = []
    arch_flags: dict[tuple[str, str], list[int]] = {}
    with writethrough.connect(bind=False) as lg:
        for r in lg.execute("SELECT key, name, type, archived FROM variation "
                            "WHERE name IS NOT NULL AND name != '' ORDER BY name COLLATE NOCASE"):
            if r["key"] in retired:                   # retired varieties are not valid targets
                continue
            nt = proj.norm(r["type"] or "")
            # VE2: a variety is frozen only when EVERY one of its format Keys is archived -- if any format is
            # still active, a listing can bind to that one, so it is not treated as archived.
            arch_flags.setdefault((proj.norm(r["name"]), nt), []).append(int(r["archived"] or 0))
            # Dedup on (name, TYPE), not name alone: the slab/block/tile copies of ONE variety share both and
            # collapse to a single target, but a legitimately multi-type name ('Coffee' = Marble AND Onyx)
            # keeps BOTH as distinct targets -- else one stone is hidden from the alias dropdown.
            key = (proj.norm(r["name"]), nt)
            if key not in seen:
                seen.add(key)
                rows.append({"name": r["name"], "stone_type": r["type"] or ""})
            live.append(Variety(r["key"], r["name"], r["type"] or ""))
    alias = _alias_map(live)
    archived = {k for k, flags in arch_flags.items() if flags and all(flags)}
    return _index_from_rows(rows, alias, archived)


def _alias_map(live: list[Variety]) -> dict[tuple[str, str], str | None]:
    """(norm alias or code, norm type) -> canonical NAME, over the SAME surfaces the matcher indexes
    (aliases.surfaces: seed, operator, the export's hand rows), so "is this a known alias" has one answer. None
    where the value sits on two distinct same-type varieties (ambiguous, never guessed)."""
    from stone_pipeline.aliases import stores as alias_stores
    from stone_pipeline.reference.medusa_files import read_variants_export
    path = alias_stores.export_file()
    # the export's own reader decides its columns; a host with no export yet has no hand rows to add
    records = read_variants_export(path, require_status=False) if path.exists() else []
    scraper = alias_stores.scraper_stores()
    stores = Stores(seed=scraper.seed, operator=scraper.operator, learned=scraper.learned,
                    admin=alias_stores.admin_of(records))
    surfaces, _ = surfaces_by_key(live, stores, alias_stores.is_code())
    hits: dict[tuple[str, str], set[str]] = {}
    for v in live:
        found = surfaces[v.key]
        for value, _ in (*found.aliases, *found.codes):
            if na := proj.norm(value):
                hits.setdefault((na, proj.norm(v.stone_type)), set()).add(v.name)
    return {k: (next(iter(names)) if len(names) == 1 else None) for k, names in hits.items()}


def _rows(q: str | None = None) -> list[dict]:
    """Existing, non-retired varieties from the ledger variation table: [{'name', 'stone_type'}], deduped by
    normalized name and sorted. `q` filters by name substring (a type-ahead). Empty when no ledger exists yet.
    Backed by the cached ledger index, so calling it many times in one pass does not re-scan the ledger."""
    rows = _index()[0]
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
    return bool(n) and n in _index()[2]


def exists_as(name: str, stone_type: str) -> bool:
    """True iff `name` already exists as a variety OF `stone_type`. Identity is (type, name), so a mint's
    operator-corrected name (mint + rename) is refused only when that exact pair exists -- 'Calacatta' as a
    Quartzite is legal beside the Marble one. Same ledger source as exists()."""
    n, t = proj.norm(name or ""), proj.norm(stone_type or "")
    return bool(n) and bool(t) and (n, t) in _index()[3]


def is_archived(name: str, stone_type: str) -> bool:
    """True iff the variety `name` of `stone_type` is FROZEN in Medusa (VE2): every one of its format Keys is
    archived. Same ledger source as exists_as (variation.archived, the PR3 mirror). False when no ledger yet or
    the export carries no status column, so decide is dormant until Blokport archives a variety."""
    n, t = proj.norm(name or ""), proj.norm(stone_type or "")
    return bool(n) and bool(t) and (n, t) in _index()[4]


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
    return _index()[1].get((n, t))
