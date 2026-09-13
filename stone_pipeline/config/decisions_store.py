"""The durable owner of the operator's review decisions.

ONE store, ONE table, ONE place for a storage bug to live. Everything the operator decides about a scraped
listing lives in config.db -- which is snapshotted + restored, so decisions survive an ECS task restart --
as STATEMENTS (DECISION_MODEL_DESIGN.md): for vendor S the spelling X is variety N of type T (colour, origin,
widen), or X is not a variety (reject). Vendor '' is the spelling's global meaning. Two callers use this
module and nothing reimplements storage:

  - the produce side READS every statement once (`load_decisions`, via stages/decisions.py) into ONE
    Decisions object every stage consults, and REWRITES the pending queue at the end;
  - the config server serves the pending queue to the admin and WRITES the operator's statements
    (`decide`, `reject`, `clear`).

Whether a statement mints or binds is derived when the object is built, from what exists then (see
config.decisions_model). The attribute ids, the per-variety origin edits, the backbone leaf verdicts and the
'not a duplicate' marks are separate facts and keep their own tables below.

Design rules (kept deliberately strict so bugs stay local):
  - reads return an EMPTY result for a fresh store (a genuine empty set) and never create the file; only a
    real DB error raises;
  - every key is NORMALIZED (pre- and post-sync stable; a pending listing has no Medusa id yet).

No em dashes in code comments is design principle 2 elsewhere; this module keeps to it.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone

from stone_pipeline.config import store
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.config.domain import active_pack
from stone_pipeline.matching import projections as proj

_ACTIONS = ("mint", "reject")
_PENDING_KINDS = ("variety", "attribute", "backbone_leaf", "origin", "decision_gap")
# The vocabularies a backbone variety carries an allowed SET of; each maps to a leaf-decision `attribute`.
# Value additions to these are what the backbone-leaf loop grows (all already in Medusa). Declared by the
# active product-domain pack (all attributes except the identity disambiguator); stone = color/finish/quality.
_LEAF_ATTRIBUTES = tuple(active_pack().leaf_attributes)
_LEAF_ACTIONS = ("approve", "reject")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(s: str) -> str:
    return proj.norm(s or "")


class UnknownVariety(KeyError):
    """A decision was requested for a variety spelling that is not in the pending queue."""


class InvalidDecision(ValueError):
    """A decision payload the store refuses: bad action, or alias without a target."""


# THE SCOPE KEY. Every statement is keyed (norm vendor, norm spelling): vendor '' is the GLOBAL level (what
# the spelling means for every vendor), a vendor is that vendor's own level. scope_key is the one builder;
# readers resolve a row vendor first, then global (config.decisions_model.Decisions).

def scope_key(source: str, spelling: str) -> tuple[str, str]:
    """The map key of a statement at `source`'s level ('' = global)."""
    return (_norm(source), _norm(spelling))


# -- statements: the ONE table -----------------------------------------------------------------------------

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
    The two ledger lookups are memoised per call: a load asks them once per distinct (name, type)."""
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
        conn.commit()
    return n


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


# -- per-variety origin edits (the "edit origins" admin action; same channel as a mint's seed_country) ----

def set_variety_origin(variety: str, stone_type: str, country_iso: str, city: str = "", county: str = "") -> None:
    """Upsert the operator-edited origin COUNTRIES for a (variety, type) -- a comma-list ("IN,IR"). Keyed by
    (normalized variety, normalized type). Overlaid onto the origin MAP at load, so the variety resolves
    against these countries (and the per-vendor gate picks from the list). The caller validates each ISO."""
    v_norm, t_norm = _norm(variety), _norm(stone_type)
    isos = [c.strip().upper() for c in (country_iso or "").split(",") if c.strip()]
    if not v_norm or not t_norm or not isos:
        raise InvalidDecision("variety origin edit requires variety, stone_type and at least one country_iso")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO variety_origin (variant_norm, stone_type_norm, variant_display, country_iso, "
            "city, county, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(variant_norm, stone_type_norm) DO UPDATE SET "
            "variant_display = excluded.variant_display, country_iso = excluded.country_iso, "
            "city = excluded.city, county = excluded.county, decided_at = excluded.decided_at",
            (v_norm, t_norm, variety.strip(), ",".join(isos), city.strip(), county.strip(), _now()))
        conn.commit()


def delete_variety_origin(variety: str, stone_type: str) -> bool:
    """Remove one variety origin edit (revert that (variety, type) to the base map). Returns True if a row
    was removed."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM variety_origin WHERE variant_norm = ? AND stone_type_norm = ?",
                         (_norm(variety), _norm(stone_type))).rowcount
        conn.commit()
        return n > 0


def variety_origins() -> dict[tuple[str, str], str]:
    """(normalized variety, normalized type) -> comma-list ISO. Overlaid onto the origin map at load
    (apply_origin_overlay), AFTER the mint seed_country overlay so an explicit edit wins. Empty for a fresh
    store."""
    with closing(store.read_store()) as conn:
        return {(r["variant_norm"], r["stone_type_norm"]): r["country_iso"]
                for r in conn.execute("SELECT variant_norm, stone_type_norm, country_iso FROM variety_origin")}


def list_variety_origins() -> list[dict]:
    """Every variety origin edit as a display row, for the admin list."""
    with closing(store.read_store()) as conn:
        return [{"ref": f"{r['variant_norm']}|{r['stone_type_norm']}", "variety": r["variant_display"],
                 "stone_type": r["stone_type_norm"], "countries": r["country_iso"].split(","),
                 "city": r["city"], "county": r["county"]}
                for r in conn.execute(
                    "SELECT variant_norm, stone_type_norm, variant_display, country_iso, city, county "
                    "FROM variety_origin ORDER BY variant_display")]


def get_variety_origin(variety: str, stone_type: str) -> dict | None:
    """The stored edit for one (variety, type), or None if unedited (the base map applies)."""
    with closing(store.read_store()) as conn:
        r = conn.execute("SELECT variant_display, country_iso, city, county FROM variety_origin "
                         "WHERE variant_norm = ? AND stone_type_norm = ?",
                         (_norm(variety), _norm(stone_type))).fetchone()
    if not r:
        return None
    return {"variety": r["variant_display"], "stone_type": _norm(stone_type),
            "countries": r["country_iso"].split(","), "city": r["city"], "county": r["county"]}


def clear_variety_origins() -> int:
    """Drop EVERY per-variety origin edit. PRISTINE reset ONLY (same rationale as clear_variety_decisions:
    a factory reset returns to the pure base, so the operator overlay is wiped). Returns rows deleted."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM variety_origin").rowcount
        conn.commit()
        return n


# -- attribute decisions (produce READS these) ---------------------------------

def attribute_ids() -> dict[tuple[str, str], tuple[str, str]]:
    """(kind, norm(value)) -> (ORIGINAL value, medusa_id) for the ids the operator pasted. The original
    value (operator casing) becomes the canonical attribute name, not its normalization."""
    with closing(store.read_store()) as conn:
        return {(r["kind"], r["value_norm"]): (r["value_display"], r["medusa_id"])
                for r in conn.execute(
                    "SELECT kind, value_norm, value_display, medusa_id FROM attribute_decision")}


def set_attribute_id(kind: str, value: str, medusa_id: str) -> None:
    kind = (kind or "").strip().lower()
    value = (value or "").strip()
    medusa_id = (medusa_id or "").strip()
    if not (kind and value and medusa_id):
        raise InvalidDecision("attribute decision needs kind, value, and medusa_id")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO attribute_decision (kind, value_norm, value_display, medusa_id, decided_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind, value_norm) DO UPDATE SET "
            "value_display = excluded.value_display, medusa_id = excluded.medusa_id, "
            "decided_at = excluded.decided_at",
            (kind, _norm(value), value, medusa_id, _now()))
        conn.commit()


def clear_attribute_ids() -> int:
    """Drop every operator-pasted attribute id. Paired with a GLOBAL ledger reset: after Medusa is wiped
    those ids point at entities that no longer exist, so clearing them makes the next catalog re-derive
    from the fresh attributes.csv export and re-prompt for any still needed, instead of adopting a dead id.
    Returns the number of rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM attribute_decision").rowcount
        conn.commit()
    return n


def clear_leaf_decisions() -> int:
    """Drop EVERY backbone leaf-growth decision (approve/reject). PRISTINE reset ONLY, same rationale as
    clear_variety_decisions: an approved leaf grows the tree via the load-time overlay (apply_leaf_overlay),
    so it must be forgotten for a cold start to reproduce the committed backbone. Returns the rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM backbone_leaf_decision").rowcount
        conn.commit()
    return n


def protected_keys() -> set[str]:
    """Variation Keys the operator marked 'not a duplicate'. The reconcile dedup must never drop these,
    so a false-positive collapse (two genuinely distinct varieties) cannot re-tombstone them. Empty set
    for a fresh store."""
    with closing(store.read_store()) as conn:
        return {r["key"] for r in conn.execute("SELECT key FROM protected_variation")}


def add_protected(key: str) -> None:
    """Record a 'not a duplicate' verdict for a variation Key (idempotent: a repeat is a no-op)."""
    with closing(store.open_store()) as conn:
        conn.execute("INSERT OR IGNORE INTO protected_variation (key, decided_at) VALUES (?, ?)",
                     (key, _now()))
        conn.commit()


def clear_protected() -> int:
    """Drop EVERY 'not a duplicate' verdict. PRISTINE reset ONLY, same rationale as clear_variety_decisions:
    a cold start back to the committed seed must forget operator dedup overrides. Returns rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM protected_variation").rowcount
        conn.commit()
    return n


# -- backbone leaf-growth decisions (produce READS the overlay + decided set) --

def backbone_leaf_overlay() -> dict[tuple[str, str], dict[str, list[str]]]:
    """(variety_norm, stone_type_norm) -> {attribute: [display values]} for APPROVED leaf additions only.
    This is the load-time overlay the backbone is grown with (loaders.Backbone.apply_leaf_overlay); the
    committed seed JSON is never touched. Empty for a fresh store (no side effect: does not create the DB),
    and rejected rows are absent (a reject is just 'do not add')."""
    out: dict[tuple[str, str], dict[str, list[str]]] = {}
    with closing(store.read_store()) as conn:
        for r in conn.execute("SELECT variety_norm, stone_type_norm, attribute, value_display "
                              "FROM backbone_leaf_decision WHERE action = 'approve'"):
            key = (r["variety_norm"], r["stone_type_norm"])
            out.setdefault(key, {}).setdefault(r["attribute"], []).append(r["value_display"])
    return out


def leaf_decided() -> set[tuple[str, str, str, str]]:
    """(variety_norm, stone_type_norm, attribute, value_norm) for every DECIDED leaf suggestion (approve
    OR reject), so the produce drops them from the pending queue -- a decided item stops reappearing."""
    with closing(store.read_store()) as conn:
        return {(r["variety_norm"], r["stone_type_norm"], r["attribute"], r["value_norm"])
                for r in conn.execute("SELECT variety_norm, stone_type_norm, attribute, value_norm "
                                      "FROM backbone_leaf_decision")}


def _leaf_actions_by_ref() -> dict[str, str]:
    """ref ('variety_norm|stone_type_norm|attribute|value_norm') -> action, for the UI to reflect a
    between-runs decision on a still-pending row (mirrors variety `current_action`)."""
    with closing(store.read_store()) as conn:
        return {"|".join((r["variety_norm"], r["stone_type_norm"], r["attribute"], r["value_norm"])): r["action"]
                for r in conn.execute("SELECT variety_norm, stone_type_norm, attribute, value_norm, action "
                                      "FROM backbone_leaf_decision")}


def set_backbone_leaf_decision(variety: str, stone_type: str, attribute: str, value: str,
                               action: str) -> None:
    """Record the operator's verdict on adding `value` (a colour/finish/quality Medusa already has) onto
    `variety` (disambiguated by stone_type). approve -> the value joins the variety's allowed set as a
    load-time overlay; reject -> never propose it again. The committed backbone seed is never touched."""
    attribute = (attribute or "").strip().lower()
    action = (action or "").strip().lower()
    if attribute not in _LEAF_ATTRIBUTES:
        raise InvalidDecision(f"leaf attribute must be one of {_LEAF_ATTRIBUTES}, got {attribute!r}")
    if action not in _LEAF_ACTIONS:
        raise InvalidDecision(f"leaf action must be approve|reject, got {action!r}")
    if not (variety.strip() and value.strip()):
        raise InvalidDecision("leaf decision needs a variety and a value")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO backbone_leaf_decision (variety_norm, stone_type_norm, attribute, value_norm, "
            "value_display, action, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(variety_norm, stone_type_norm, attribute, value_norm) DO UPDATE SET "
            "value_display = excluded.value_display, action = excluded.action, decided_at = excluded.decided_at",
            (_norm(variety), _norm(stone_type), attribute, _norm(value), value.strip(), action, _now()))
        conn.commit()


def _decide_leaf_from_payload(payload: dict, action: str) -> None:
    set_backbone_leaf_decision(payload.get("variety", ""), payload.get("stone_type", ""),
                               payload.get("attribute", ""), payload.get("add_value", ""), action)


def approve_leaf_pending(verdict: str | None = None) -> int:
    """Bulk-approve the still-UNDECIDED pending backbone-leaf suggestions, optionally only those with
    `verdict` (e.g. 'likely_real'). Never re-flips a row the operator already decided (approve or reject).
    Returns the count approved. The approvals apply to the tree on the next produce (the standard
    'applies next produce' contract), and the decided rows drop off the queue on that run."""
    n = 0
    for item in list_pending("backbone_leaf"):
        if item.get("current_action") is not None:      # already decided; do not re-flip
            continue
        if verdict and item.get("verdict") != verdict:
            continue
        _decide_leaf_from_payload(item, "approve")
        n += 1
    return n


def decide_leaf_pending(ref: str, action: str) -> bool:
    """Record one operator verdict (approve|reject) on the pending backbone-leaf suggestion identified by
    `ref`. Reads the queued payload so the client need not resend the display fields. Returns False if no
    such pending item (already decided or unknown ref)."""
    payload = pending_payload("backbone_leaf", ref)
    if payload is None:
        return False
    _decide_leaf_from_payload(payload, action)
    return True


def list_decided_leaves() -> list[dict]:
    """Every recorded backbone-leaf verdict (approve OR reject), newest first, each carrying the `ref` the
    review PUT takes -- so an operator can find and revise a past decision (e.g. undo a spurious approval a
    prior run already grew into the overlay). Empty for a fresh store, with no side effect (does not create
    the DB)."""
    with closing(store.read_store()) as conn:
        rows = conn.execute(
            "SELECT variety_norm, stone_type_norm, attribute, value_norm, value_display, action, decided_at "
            "FROM backbone_leaf_decision ORDER BY decided_at DESC, value_display").fetchall()
    return [{"variety": r["variety_norm"], "stone_type": r["stone_type_norm"], "attribute": r["attribute"],
             "add_value": r["value_display"], "action": r["action"], "decided_at": r["decided_at"],
             "ref": "|".join((r["variety_norm"], r["stone_type_norm"], r["attribute"], r["value_norm"]))}
            for r in rows]


def revise_leaf_decision(ref: str, action: str) -> bool:
    """Change or remove an ALREADY-decided backbone leaf, by `ref`. decide_leaf_pending only reaches a leaf
    still in the pending queue; once decided, a leaf drops off that queue, so revising it works off the
    recorded decision instead. `action`:
      approve|reject -> flip the verdict (un-approving a grown leaf: approve -> reject drops it from the
                        overlay AND keeps it decided, so it is never re-suggested);
      clear          -> delete the verdict, returning the leaf to pristine (it stops growing the tree; the
                        suggestion re-surfaces on a later produce only if a product still implies it).
    Returns False if `ref` names no decided leaf. The change applies on the next produce, like every other
    decision (the overlay is read at load_backbone)."""
    action = (action or "").strip().lower()
    if action not in ("approve", "reject", "clear"):
        raise InvalidDecision(f"leaf revision must be approve|reject|clear, got {action!r}")
    parts = ref.split("|")
    if len(parts) != 4:
        return False
    where = "WHERE variety_norm = ? AND stone_type_norm = ? AND attribute = ? AND value_norm = ?"
    with closing(store.read_store()) as conn:
        if action == "clear":
            cur = conn.execute(f"DELETE FROM backbone_leaf_decision {where}", tuple(parts))
        else:
            cur = conn.execute(f"UPDATE backbone_leaf_decision SET action = ?, decided_at = ? {where}",
                               (action, _now(), *parts))
        conn.commit()
    return cur.rowcount > 0


# -- the pending review queue (produce WRITES it, the API READS it) -------------

def replace_pending(kind: str, rows: list[dict]) -> None:
    """Wholly replace the pending queue for `kind` ('variety'|'attribute'|'backbone_leaf') with this
    produce's undecided items. A decided item simply is not in `rows`, so it stops appearing. Each row must
    carry `ref` (the stable key) and `payload` (the JSON-able dict the UI renders); `sources` (list) is
    optional provenance."""
    if kind not in _PENDING_KINDS:
        raise InvalidDecision(f"pending kind must be one of {_PENDING_KINDS}, got {kind!r}")
    now = _now()
    # Two surfaced items can share a ref: norm(name) collides (e.g. 'Imperial White' typed granite AND
    # quartzite clean to the same name; 'Blue-Carara' vs 'Blue Carara' likewise). A decision is keyed by
    # that normalized name, so the two are ONE queue entry -- collapse them (keep last, matching the old
    # name-keyed dict-overwrite) instead of hitting the (kind, ref) primary key with a duplicate INSERT.
    deduped = {r["ref"]: r for r in rows}
    with closing(store.open_store()) as conn:
        conn.execute("DELETE FROM review_pending WHERE kind = ?", (kind,))
        conn.executemany(
            "INSERT INTO review_pending (kind, ref, payload, sources, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(kind, r["ref"], json.dumps(r["payload"]),
              json.dumps(r["sources"]) if r.get("sources") is not None else None, now)
             for r in deduped.values()])
        conn.commit()


def clear_review_pending(kinds: tuple[str, ...] = _PENDING_KINDS) -> int:
    """Empty the review queue(s) so a GLOBAL clean-start reset gives an immediately blank slate, instead of
    the pre-reset queue lingering until the next produce recomputes (replace_pending) it. Returns the number
    of rows dropped. Global-only, like the ledger reset's shared base layer -- a scoped per-source reset
    leaves the queue (and the rest of config.db) alone."""
    for k in kinds:
        if k not in _PENDING_KINDS:
            raise InvalidDecision(f"pending kind must be one of {_PENDING_KINDS}, got {k!r}")
    marks = ",".join("?" * len(kinds))
    with closing(store.open_store()) as conn:
        n = conn.execute(f"DELETE FROM review_pending WHERE kind IN ({marks})", tuple(kinds)).rowcount
        conn.commit()
    return n


def pending_payload(kind: str, ref: str) -> dict | None:
    """The stored payload for one pending item, or None. Lets the API act on a queued suggestion by its
    ref without the client resending the display fields."""
    with closing(store.read_store()) as conn:
        r = conn.execute("SELECT payload FROM review_pending WHERE kind = ? AND ref = ?",
                         (kind, ref)).fetchone()
    return json.loads(r["payload"]) if r else None


def _project_decision(item: dict, dec: Decisions, with_statements: bool = True) -> None:
    """Overlay the operator's standing decision onto ONE pending card, in place. This is the single source of
    the `current_*` view every restatement card (variety AND origin) shows, so the two kinds derive their
    editable decision identically and cannot drift apart. It reads the one decisions object:

      * `dec.bindings`   -- the per-vendor bind {(nsrc, nspell): (target variety, target type)}; the card's
                            own (source, scraped) listings are the keys, with a single-vendor fallback;
      * `dec.statements` -- the mint/reject statement per scope key (a reject is keyed by the card ref);
                            `with_statements=False` for a kind that has none (origin), so the view is driven
                            by the bind alone.

    From those it sets decided, current_action, current_alias_of and current_seed_name/type/colour/country,
    backfills an empty scraped type from the decided type (so a decided card stays re-keyable), and resolves
    the per-decision widen (`dec.widened`, the "documented origin" checkbox) keyed on (source, target, type).
    The card always reflects the LAST decision: a per-vendor bind supersedes a stale mint that the
    source-scoped clear could not drop, unless that mint's own rename points at the same bind (mint+rename is
    one decision, still read as the mint)."""
    keys = [(_norm(l.get("source", "")), _norm(l.get("scraped", "")))
            for l in (item.get("listings") or []) if isinstance(l, dict)]
    # single-vendor fallback: a variety card carries the vendor as `src`, an origin card as `source` (and no
    # listings in its stored payload), so accept either as the (vendor, scraped) key the bind was stored under
    if item.get("scraped"):
        if item.get("src"):
            keys.append((_norm(item["src"]), _norm(item["scraped"])))
        if item.get("source"):
            keys.append((_norm(item["source"]), _norm(item["scraped"])))
    hit = next((dec.bindings[k] for k in keys if k in dec.bindings), None)       # (target variety, target type)
    # a mint/rename/reject is keyed on a scraped spelling (or the cleaned ref for a reject), never guaranteed
    # to equal the card ref, so match on every spelling the card carries
    spellings = {_norm(s) for s in (item.get("spellings") or []) if s}
    if item.get("scraped"):
        spellings.add(_norm(item["scraped"]))
    spellings |= {_norm(l.get("scraped", "")) for l in (item.get("listings") or [])
                  if isinstance(l, dict) and l.get("scraped")}
    st = None
    if with_statements:
        statements = dec.statements
        # a reject is keyed on the card's NAME (its ref is name|type for a typed card), so look it up by the name
        st = (next((statements[k] for k in keys if k in statements), None)               # the vendor's own level
              or statements.get(scope_key("", (item.get("variant") or item.get("ref") or "").split("|")[0]))
              or next((statements[scope_key("", s)] for s in spellings if scope_key("", s) in statements), None))
    if st is not None and st.is_mint and hit is not None and _norm(st.name or "") != _norm(hit[0]):
        st = None                                                    # stale mint superseded by the newer bind
    action = None if st is None else ("mint" if st.is_mint else "reject")
    item["decided"] = action is not None or hit is not None
    item["current_action"] = action or ("alias" if hit else None)
    item["current_alias_of"] = hit[0] if hit else None
    item["current_seed_color"] = st.color if st is not None and st.is_mint else None
    item["current_seed_type"] = (st.stone_type if st is not None and st.is_mint else None) or (hit[1] if hit else None) or None
    item["current_seed_country"] = st.origin_iso if st is not None and st.is_mint else None
    item["current_seed_name"] = st.name if st is not None and st.is_mint else None
    if item["decided"] and not item.get("stone_type") and item["current_seed_type"]:
        item["stone_type"] = item["current_seed_type"]              # re-keyable: fill an empty scraped type
    tgt_n = _norm(item.get("current_alias_of") or item.get("current_seed_name")
                  or item.get("variant") or item.get("variety", ""))
    tgt_t = _norm(item.get("current_seed_type") or item.get("stone_type", ""))
    srcs = {_norm(l.get("source", "")) for l in (item.get("listings") or []) if isinstance(l, dict)}
    if item.get("src"):
        srcs.add(_norm(item["src"]))
    if item.get("source"):
        srcs.add(_norm(item["source"]))
    doc = next((dec.widened[(s, tgt_n, tgt_t)] for s in srcs if (s, tgt_n, tgt_t) in dec.widened), None)
    item["documented_origin"] = doc
    item["widen"] = doc is not None


def list_pending(kind: str) -> list[dict]:
    """The pending items for `kind`, each = its payload plus `sources` and the `current_action` already
    recorded for it (so the UI can show a decision made between runs, applied on the next produce)."""
    # Match a decision to a card WITHOUT relying on the card ref. The ref is the CLEANED variety name (the
    # cleaner strips a trailing type word: "Amazon Green Granite" -> ref "amazon green"), but decide() keys
    # a statement on the SCRAPED spelling ("amazon green granite"). So the flag has to match on the
    # card's own scraped spellings, not its ref -- otherwise every card whose cleaner stripped a suffix
    # comes back undecided even though the bind is stored (the Gold-card bug). Reject is the exception: it
    # is keyed on the ref, so ref is kept as a candidate too.
    dec = load_decisions() if kind in ("variety", "origin") else Decisions.empty()
    leaf_actions = _leaf_actions_by_ref() if kind == "backbone_leaf" else {}
    # for origin, key the confirmed country by the SAME composite ref the queue uses, so a decision made
    # between runs shows as current_country until the next produce regenerates the queue (and drops it).
    origin_actions = ({f"{s}|{v}|{t}": iso for (s, v, t), iso in dec.vendor_origins.items()}
                      if kind == "origin" else {})
    with closing(store.read_store()) as conn:
        rows = conn.execute(
            "SELECT ref, payload, sources FROM review_pending WHERE kind = ? ORDER BY ref", (kind,)
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        item = json.loads(r["payload"])
        # Surface the stable queue key the review PUT takes (/review/{kind}/<ref>). The payload holds only
        # display fields; the composite backbone-leaf ref (variety_norm|stone_type_norm|attribute|value_norm)
        # cannot be reconstructed client-side from those, so without this the UI has no valid ref to send and
        # correctly disables approve/reject. list_decided_leaves already exposes ref; pending now matches.
        item["ref"] = r["ref"]
        item["sources"] = json.loads(r["sources"]) if r["sources"] else []
        if kind == "variety":
            # a variety statement carries a mint/rename/reject AND may carry a bind
            _project_decision(item, dec)
        elif kind == "origin":
            # an origin card has NO mint/reject of its own -- its decision is the bind (+ the confirmed
            # country), so the SAME projection drives it with statements off; the country is origin-specific
            _project_decision(item, dec, with_statements=False)
            item["current_country"] = origin_actions.get(r["ref"])
            # a card settled purely by confirming the country (no bind) still reads decided, action "origin"
            if origin_actions.get(r["ref"]) is not None:
                item["decided"] = True
                item["current_action"] = item.get("current_action") or "origin"
        elif kind == "backbone_leaf":
            item["current_action"] = leaf_actions.get(r["ref"])
            item["decided"] = leaf_actions.get(r["ref"]) is not None
        out.append(item)
    return out
