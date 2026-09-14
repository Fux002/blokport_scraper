"""'Not a duplicate' verdicts: variation Keys the operator marked so the reconcile dedup must never drop
them, so a false-positive collapse (two genuinely distinct varieties) cannot re-tombstone them."""

from __future__ import annotations

from contextlib import closing

from stone_pipeline.config import store

from ._common import _now


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
    """Drop EVERY 'not a duplicate' verdict. PRISTINE reset ONLY: a cold start back to the committed seed
    must forget operator dedup overrides. Returns rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM protected_variation").rowcount
        conn.commit()
    return n
