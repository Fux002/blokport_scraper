"""Backbone leaf-growth decisions: the operator's verdict on adding a colour/finish/quality value (one Medusa
already has) onto a variety's allowed set. approve grows the tree via the load-time overlay
(loaders.Backbone.apply_leaf_overlay); the committed backbone seed is never touched. Produce READS the overlay
and the decided set; the config server records the verdicts.

A pure table module: it depends on nothing else in the package. The "act on a PENDING leaf" workflows
(approve_leaf_pending / decide_leaf_pending) are queue consumers and live in queue.py, so the dependency runs
one way (queue -> leaves), never a cycle."""

from __future__ import annotations

from contextlib import closing

from stone_pipeline.config import store
from stone_pipeline.config.domain import active_pack

from ._common import InvalidDecision, _norm, _now

# The vocabularies a backbone variety carries an allowed SET of; each maps to a leaf-decision `attribute`.
# Value additions to these are what leaf-growth grows (all already in Medusa). Declared by the active
# product-domain pack (all attributes except the identity disambiguator); stone = color/finish/quality.
_LEAF_ATTRIBUTES = tuple(active_pack().leaf_attributes)
_LEAF_ACTIONS = ("approve", "reject")


def clear_leaf_decisions() -> int:
    """Drop EVERY backbone leaf-growth decision (approve/reject). PRISTINE reset ONLY: an approved leaf grows
    the tree via the load-time overlay, so it must be forgotten for a cold start to reproduce the committed
    backbone. Returns the rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM backbone_leaf_decision").rowcount
        conn.commit()
    return n


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
