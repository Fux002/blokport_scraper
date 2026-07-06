"""The durable owner of new-variant review decisions.

ONE store, ONE place for a storage bug to live. Everything the operator decides about an uncertain
variety (mint / reject / alias-to-existing) and every new-attribute id they paste lives in config.db
-- which is already snapshotted + restored, so these decisions survive an ECS task restart (unlike the
old ephemeral CSVs under /app). Two callers use this module and nothing reimplements storage:

  - the produce side (`stages/decisions.py`) READS decisions at the start of a run and REWRITES the
    pending queue at the end;
  - the config server (`config/server.py`) serves the pending queue to the :4200 admin and WRITES the
    operator's decisions back.

Design rules (kept deliberately strict so bugs stay local):
  - `action` unifies the two old CSVs: mint == the old `confirm=true`, reject == `confirm=false` AND the
    learned reject memory, alias == the new "this is really a spelling of X" action.
  - reads return an EMPTY result for a fresh store (a genuine empty set), and only RAISE on a real DB
    error -- there is no "file missing -> silently return {}" fallback path.
  - varieties are keyed by NORMALIZED name (pre- and post-sync stable; a pending variety has no Medusa
    id yet), attributes by (kind, normalized value).

No em dashes in code comments is design principle 2 elsewhere; this module keeps to it.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone

from stone_pipeline.config import store
from stone_pipeline.matching import projections as proj

_ACTIONS = ("mint", "reject", "alias")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(s: str) -> str:
    return proj.norm(s or "")


class UnknownVariety(KeyError):
    """A decision was requested for a variety spelling that is not in the pending queue."""


class InvalidDecision(ValueError):
    """A decision payload the store refuses: bad action, or alias without a target."""


# -- variety decisions (produce READS these) -----------------------------------

def variety_actions() -> dict[str, dict]:
    """norm(variant) -> {'action': mint|reject|alias, 'alias_of': str|None} for every decided variety.
    Empty for a fresh store."""
    with closing(store.open_store()) as conn:
        return {r["variant_norm"]: {"action": r["action"], "alias_of": r["alias_of"]}
                for r in conn.execute("SELECT variant_norm, action, alias_of FROM variety_decision")}


def confirm_map() -> dict[str, str]:
    """norm(variant) -> 'yes'|'no' -- the mint/reject view the legacy confirm-file reader expects.
    alias decisions are NOT in this map; the alias router consumes them separately (`alias_map`)."""
    out: dict[str, str] = {}
    for name, dec in variety_actions().items():
        if dec["action"] == "mint":
            out[name] = "yes"
        elif dec["action"] == "reject":
            out[name] = "no"
    return out


def rejected_names() -> set[str]:
    """norm(variant) for every reject decision -- the learned 'never propose again' memory."""
    return {n for n, d in variety_actions().items() if d["action"] == "reject"}


def alias_map() -> dict[str, str]:
    """norm(spelling) -> alias_of (the target variety NAME) for every alias decision. The router adds
    the spelling to the target variety's alias set so the product resolves onto it."""
    return {n: d["alias_of"] for n, d in variety_actions().items()
            if d["action"] == "alias" and d["alias_of"]}


def set_variety_decision(variant: str, action: str, alias_of: str | None = None) -> None:
    """Upsert ONE operator decision. Raises InvalidDecision on a bad action or an alias with no target.
    Idempotent: re-deciding a variety overwrites the prior decision."""
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        raise InvalidDecision(f"action must be one of {_ACTIONS}, got {action!r}")
    alias_of = (alias_of or "").strip() or None
    if action == "alias" and not alias_of:
        raise InvalidDecision("alias decision requires alias_of (an existing variety name)")
    if action != "alias":
        alias_of = None                      # only alias carries a target; keep the row unambiguous
    norm = _norm(variant)
    if not norm:
        raise InvalidDecision("variant name is empty")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO variety_decision (variant_norm, variant_display, action, alias_of, decided_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(variant_norm) DO UPDATE SET "
            "variant_display = excluded.variant_display, action = excluded.action, "
            "alias_of = excluded.alias_of, decided_at = excluded.decided_at",
            (norm, variant.strip(), action, alias_of, _now()))
        conn.commit()


def learn_rejects(names: set[str]) -> None:
    """Persist runtime-learned rejects (curate marks a code-shaped name it was told 'no' on). INSERT OR
    IGNORE so it never overwrites an explicit mint/alias decision the operator made."""
    if not names:
        return
    now = _now()
    with closing(store.open_store()) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO variety_decision "
            "(variant_norm, variant_display, action, alias_of, decided_at) VALUES (?, ?, 'reject', NULL, ?)",
            [(_norm(n), n, now) for n in names if _norm(n)])
        conn.commit()


# -- attribute decisions (produce READS these) ---------------------------------

def attribute_ids() -> dict[tuple[str, str], tuple[str, str]]:
    """(kind, norm(value)) -> (ORIGINAL value, medusa_id) for the ids the operator pasted. The original
    value (operator casing) becomes the canonical attribute name, not its normalization."""
    with closing(store.open_store()) as conn:
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


# -- the pending review queue (produce WRITES it, the API READS it) -------------

def replace_pending(kind: str, rows: list[dict]) -> None:
    """Wholly replace the pending queue for `kind` ('variety'|'attribute') with this produce's undecided
    items. A decided item simply is not in `rows`, so it stops appearing. Each row must carry `ref` (the
    stable key) and `payload` (the JSON-able dict the UI renders); `sources` (list) is optional provenance."""
    if kind not in ("variety", "attribute"):
        raise InvalidDecision(f"pending kind must be variety|attribute, got {kind!r}")
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


def list_pending(kind: str) -> list[dict]:
    """The pending items for `kind`, each = its payload plus `sources` and the `current_action` already
    recorded for it (so the UI can show a decision made between runs, applied on the next produce)."""
    actions = variety_actions() if kind == "variety" else {}
    with closing(store.open_store()) as conn:
        rows = conn.execute(
            "SELECT ref, payload, sources FROM review_pending WHERE kind = ? ORDER BY ref", (kind,)
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        item = json.loads(r["payload"])
        item["sources"] = json.loads(r["sources"]) if r["sources"] else []
        if kind == "variety":
            item["current_action"] = actions.get(r["ref"], {}).get("action")
            item["current_alias_of"] = actions.get(r["ref"], {}).get("alias_of")
        out.append(item)
    return out
