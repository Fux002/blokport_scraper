"""The decision model proper: the ONE `statement` table, its reader/builder, and the writers.

For vendor S the spelling X is variety N of type T (colour, origin, widen), or X is not a variety (reject).
Vendor '' is the spelling's global meaning. Whether a statement mints or binds is derived when the object is
built (config.decisions_model), from what exists then, never stored here. Reads return an EMPTY result for a
fresh store and never create the file; every key is NORMALIZED (pre- and post-sync stable)."""

from __future__ import annotations

from contextlib import closing

from stone_pipeline.config import store
from stone_pipeline.config.decisions_model import Decisions

from ._common import InvalidDecision, _norm, _now


# THE SCOPE KEY. Every statement is keyed (norm vendor, norm spelling): vendor '' is the GLOBAL level (what
# the spelling means for every vendor), a vendor is that vendor's own level. scope_key is the one builder;
# readers resolve a row vendor first, then global (config.decisions_model.Decisions).
def scope_key(source: str, spelling: str) -> tuple[str, str]:
    """The map key of a statement at `source`'s level ('' = global)."""
    return (_norm(source), _norm(spelling))


_STATEMENT_COLS = "source, spelling_norm, spelling, verdict, name, stone_type, color, origin_iso, widen, asked_by"


def _statement_rows() -> list[dict]:
    with closing(store.read_store()) as conn:
        return [dict(r) for r in conn.execute(f"SELECT {_STATEMENT_COLS} FROM statement "
                                              "ORDER BY source, spelling_norm").fetchall()]


def _upsert_statement(source: str, spelling: str, verdict: str, *, name: str | None = None,
                      stone_type: str | None = None, color: str | None = None, origin_iso: str | None = None,
                      widen: bool = False, asked_by: str = "") -> None:
    src, sp = (source or "").strip(), (spelling or "").strip()
    if not sp:
        raise InvalidDecision("a statement needs a scraped spelling")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO statement (source, spelling_norm, spelling, verdict, name, stone_type, color, origin_iso, "
            "widen, asked_by, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, spelling_norm) DO UPDATE SET spelling = excluded.spelling, "
            "verdict = excluded.verdict, name = excluded.name, stone_type = excluded.stone_type, "
            "color = excluded.color, origin_iso = excluded.origin_iso, widen = excluded.widen, "
            "asked_by = excluded.asked_by, decided_at = excluded.decided_at",
            (src, _norm(sp), sp, verdict, (name or "").strip() or None, (stone_type or "").strip() or None,
             (color or "").strip() or None, (origin_iso or "").strip().upper() or None, 1 if widen else 0,
             (asked_by or src or "").strip(), _now()))
        conn.commit()


def _lookups(exists_as, alias_target, clean=None) -> tuple:
    """The variety lookups a statement is judged against, injected by tests and resolved here otherwise:
    exists_as / alias_target read the ledger (config.varieties), clean is the matcher's cleaner (adapters.tokens).
    Resolved per call, not at import, so this store stays free of the pipeline's settings and adapter packages.

    The default ledger lookups (config.varieties) read a per-ledger cached index, so a load asking them once
    per statement does ONE ledger scan, not one per name (the O(statements x scan) hang this replaces).
    Injected lookups (tests) are memoised per (name, type)."""
    if exists_as is None or alias_target is None:
        from stone_pipeline.config import varieties
        exists_as, alias_target = exists_as or varieties.exists_as, alias_target or varieties.alias_target
    if clean is None:
        from stone_pipeline.adapters.tokens import clean_variety as clean
    seen_e: dict = {}
    seen_a: dict = {}

    def _exists(n, t):
        k = (_norm(n), _norm(t))
        if k not in seen_e:
            seen_e[k] = exists_as(n, t)
        return seen_e[k]

    def _alias(n, t):
        k = (_norm(n), _norm(t))
        if k not in seen_a:
            seen_a[k] = alias_target(n, t)
        return seen_a[k]
    return _exists, _alias, clean


def load_decisions(exists_as=None, alias_target=None) -> Decisions:
    """EVERY operator statement as one object (config.decisions_model.Decisions), read once per produce. Empty
    on a fresh store (never materialises config.db from a read). Mint-vs-bind is derived here from what exists
    (the ledger, via the injected or default lookups)."""
    if not store.config_db_path().exists():
        return Decisions.empty()
    exists_as, alias_target, _ = _lookups(exists_as, alias_target)
    return Decisions.from_statements(_statement_rows(), exists_as, alias_target)


def reject(source: str, spelling: str) -> None:
    """The listing is not a variety. `source` '' rejects the spelling for every vendor (a card with no
    listings); a vendor rejects it for that vendor's listing only. The last statement on a listing wins."""
    _upsert_statement(source, spelling, "reject", asked_by=source)


def clear(source: str, spelling: str) -> int:
    """Undo ONE vendor's statement on a spelling ('' = the global one). Returns rows dropped (0 or 1)."""
    sp = _norm(spelling)
    if not sp:
        raise InvalidDecision("clearing a statement requires the scraped spelling")
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM statement WHERE source = ? AND spelling_norm = ?",
                         ((source or "").strip(), sp)).rowcount
        conn.commit()
    return n


def clear_for_variety(name: str, stone_type: str = "") -> int:
    """Drop every 'is' statement whose result is the variety (name, type): used by unmint, so a removed variety
    does not re-mint on the next produce and its listings resurface undecided. `stone_type` may be the
    canonical type or the Key type-slug ('dolomite_marble'); empty matches every type (legacy callers).
    Returns rows dropped."""
    norm = _norm(name)
    if not norm:
        return 0
    type_norm = _norm((stone_type or "").replace("_", " "))
    with closing(store.open_store()) as conn:
        doomed = [(r["source"], r["spelling_norm"]) for r in conn.execute(
            "SELECT source, spelling_norm, spelling, name, stone_type FROM statement WHERE verdict = 'is'")
                  if _norm(r["name"] or r["spelling"]) == norm
                  and (not type_norm or not r["stone_type"] or _norm(r["stone_type"].replace("_", " ")) == type_norm)]
        n = 0
        for src, sp in doomed:
            n += conn.execute("DELETE FROM statement WHERE source = ? AND spelling_norm = ?", (src, sp)).rowcount
        # the variety's durable backbone membership (leaves.record_minted_membership) rode beside its mint
        # statement; drop it in the SAME transaction so unmint can never leave a stranded membership record
        # (or its leaf overlay) behind -- one delete, the two can never diverge
        if type_norm:
            conn.execute("DELETE FROM backbone_leaf_decision WHERE variety_norm = ? AND stone_type_norm = ?",
                         (norm, type_norm))
        conn.commit()
    return n


def mint_seed_colors() -> dict[tuple[str, str], str]:
    """(norm variety name, norm stone_type) -> the operator's seed COLOUR, for every 'is' statement that set
    one. The variety is the statement's corrected name (a rename) else its spelling. A drift heal prefers
    this operator decision over any scrape-derived colour, so a backfill never overwrites what the operator
    stated. Empty for a fresh store."""
    with closing(store.read_store()) as conn:
        return {(_norm(r["name"] or r["spelling"]), _norm(r["stone_type"] or "")): r["color"]
                for r in conn.execute(
                    "SELECT spelling, name, stone_type, color FROM statement "
                    "WHERE verdict = 'is' AND color IS NOT NULL AND color != '' AND stone_type IS NOT NULL "
                    "AND stone_type != ''")}


def clear_all_statements() -> int:
    """Drop EVERY statement. PRISTINE (factory) reset ONLY: a normal soft/hard reset KEEPS the operator's
    decisions on purpose (curation you want to survive a sync reset), but a cold start back to the committed
    seed must forget them, else a previously stated variety silently re-applies on the next produce and the
    'clean' catalog is not seed-only. Returns rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM statement").rowcount
        conn.commit()
    return n


# -- decide: ONE operator statement ------------------------------------------------------------------------

def _spelling_means(spelling: str, stone_type: str, exists_as, alias_target, clean) -> str | None:
    """The existing variety a scraped spelling ALREADY means for every vendor: itself when it is an existing
    variety of `stone_type`, else the variety it is a known alias of; checked on the spelling and on its cleaned
    form, the two surfaces the matcher binds through ('Black Wave Marble' -> 'Black Wave' -> alias of Silver
    Waves). None when the spelling resolves to nothing, so a mint on it defines its global meaning."""
    for s in dict.fromkeys((spelling, clean(spelling, stone_type))):
        if exists_as(s, stone_type):
            return s
        target = alias_target(s, stone_type)
        if target:
            return target
    return None


def _global_meaning(spelling: str, stone_type: str, exists_as, alias_target, clean) -> tuple[str, str] | None:
    """(name, type) the spelling means for every vendor today: the global statement on it, else the existing
    variety it resolves to (see _spelling_means), else None (the spelling is undefined: the next mint defines it)."""
    with closing(store.read_store()) as conn:
        prior = conn.execute("SELECT spelling, name, stone_type FROM statement WHERE source = '' "
                             "AND spelling_norm = ? AND verdict = 'is'", (_norm(spelling),)).fetchone()
    if prior:
        return (prior["name"] or prior["spelling"], prior["stone_type"] or "")
    existing = _spelling_means(spelling, stone_type, exists_as, alias_target, clean)
    return (existing, stone_type) if existing else None


def decide(source: str, scraped: str, name: str, stone_type: str, color: str = "", origin: str = "",
           widen: bool = False, exists_as=None, alias_target=None, clean=None) -> dict:
    """ONE operator statement about a vendor's product -- "this is `name`, a `stone_type`, `color`, from
    `origin`" -- stored as ONE row. The operator never picks mint / alias / origin: what the statement DOES is
    derived when the decisions are loaded (config.decisions_model): an existing (name, type), or a known alias
    of one, binds the vendor's listing; anything else mints. What is decided HERE is the LEVEL:

      * a spelling means ONE variety for every vendor: the global statement on it, or the existing variety it
        already resolves to. A statement on an undefined spelling defines that meaning (global): its products
        bind everywhere, a rename attaches the spelling as an alias for everyone.
      * a statement that restates the global meaning changes nothing; one that names a DIFFERENT stone is
        that vendor's own, kept beside the global meaning and bound by its vendor binding, so no other vendor
        moves.
      * widen -> the origin is also added to the variety's documented origins (every vendor's gate).

    `exists_as(name, stone_type) -> bool`, `alias_target(name, stone_type) -> canonical name | None` and
    `clean(spelling, stone_type) -> str` are the variety lookups (see _lookups), injected so the rule is
    testable without the ledger on disk. The caller validates the vocabulary (type, ISO) first."""
    src = (source or "").strip()
    spelling = (scraped or "").strip()
    name = (name or "").strip()
    stone_type = (stone_type or "").strip()
    color = (color or "").strip()
    origin = (origin or "").strip().upper()
    if not src or not spelling or not name or not stone_type:
        raise InvalidDecision("a decision requires source, scraped spelling, name and stone_type")
    exists_as, alias_target, clean = _lookups(exists_as, alias_target, clean)
    # a name the operator states may already be an existing variety's ALIAS (same type); it then resolves to
    # that variety's canonical name, so the statement binds instead of minting a duplicate of a known alias
    resolved_alias = False
    if not exists_as(name, stone_type):
        canonical = alias_target(name, stone_type)
        if canonical:
            name, resolved_alias = canonical, True
    outcome: dict = {"source": src, "scraped": spelling, "name": name, "stone_type": stone_type,
                     "color": color or None, "origin": origin or None}
    # a statement REPLACES the vendor's previous one for this spelling, and supersedes a global REJECT on the
    # same listing (the reject is keyed by the card ref, the cleaned name): last operator action wins
    clear(src, spelling)
    for s in dict.fromkeys((spelling, clean(spelling, stone_type))):
        with closing(store.open_store()) as conn:
            conn.execute("DELETE FROM statement WHERE source = '' AND verdict = 'reject' AND spelling_norm = ?",
                         (_norm(s),))
            conn.commit()
    if exists_as(name, stone_type) or resolved_alias:
        _upsert_statement(src, spelling, "is", name=name, stone_type=stone_type, color=color,
                          origin_iso=origin, widen=widen, asked_by=src)
        outcome["result"] = "bound"
        if resolved_alias:
            outcome["resolved_alias"] = True
        return outcome
    renamed = _norm(name) != _norm(spelling)
    meaning = _global_meaning(spelling, stone_type, exists_as, alias_target, clean)
    if meaning and _norm(meaning[0]) == _norm(name) and _norm(meaning[1]) == _norm(stone_type):
        outcome["result"], outcome["level"] = "minted", "global"      # restates the global meaning: nothing new
        if origin:                                                    # ...but the vendor's origin is recorded
            _upsert_statement(src, spelling, "is", name=name, stone_type=stone_type, color=color,
                              origin_iso=origin, widen=widen, asked_by=src)
        return outcome
    level = src if meaning else ""
    _upsert_statement(level, spelling, "is", name=name if renamed or level else None, stone_type=stone_type,
                      color=color, origin_iso=origin, widen=widen, asked_by=src)
    outcome["result"], outcome["level"] = "minted", ("vendor" if level else "global")
    if widen and origin:
        outcome["widen"] = True
    return outcome
