"""New colour/finish/type/quality VALUES the operator created in Medusa and pasted the id for, keyed by
(kind, normalized value). The next produce adopts the id into the attribute vocab (produce READS these)."""

from __future__ import annotations

from contextlib import closing

from stone_pipeline.config import store

from ._common import InvalidDecision, _norm, _now


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
