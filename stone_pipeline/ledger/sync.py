"""Sync service over the ledger (SYNC_LEDGER_DESIGN.md section 8), scraper side.

This first slice is GET /sync/status: the per-type, per-state summary the backend
pull job and operators read to see what the ledger holds and what is pending sync.

The ready-payload (GET /sync/<type>) and ack (POST /sync/ack) endpoints (8.2 to 8.4)
land next; they need the agreed backend contract plus a little more ledger
groundwork (canonical stone type on variations, materialized combinations, and
image-promotion state), none of which the shadow write-through populates yet. Kept
transport-free here on purpose: this is the logic an HTTP layer will wrap once the
transport is agreed. No em dashes (design principle 2).
"""

from __future__ import annotations

from stone_pipeline.ledger.db import Ledger

# tables that carry a sync `state` column (combination is empty until materialized)
_STATE_TABLES = ("attribute", "variation", "combination", "product", "gap")


def status(ledger: Ledger) -> dict[str, dict[str, int]]:
    """Per-type state counts for GET /sync/status.

    State-bearing tables report their state histogram (pending/dirty/synced/...).
    Inventory has no state: it reports total rows and the `delta` count (stock that
    moved since the last sync, qty distinct from last_synced_qty), which is what the
    inventory lane would serve.
    """
    out: dict[str, dict[str, int]] = {t: ledger.counts(t) for t in _STATE_TABLES}
    inv = ledger.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN last_synced_qty IS NULL OR last_synced_qty != qty THEN 1 ELSE 0 END) AS delta "
        "FROM inventory"
    ).fetchone()
    out["inventory"] = {"total": inv["total"] or 0, "delta": inv["delta"] or 0}
    return out


def main(argv: list[str] | None = None) -> int:
    import json

    from stone_pipeline.ledger import writethrough

    path = writethrough.ledger_path()
    if not path.exists():
        print(f"no ledger at {path} (run with BLOKPORT_LEDGER_WRITETHROUGH=1 first)")
        return 1
    with Ledger.open(path, env=writethrough.ENV_NAME) as ledger:
        print(json.dumps(status(ledger), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
