"""Stand-in for Medusa's pull-and-apply job, to prove the glue end to end.

The real glue is the backend's pull job calling the sync endpoints (server.py); this
module is the same loop with a fake apply, so the scraper side can be driven to
convergence without a live Medusa. It pulls ready entities, mints a placeholder id
(as Medusa would), acks it back, and repeats until nothing is ready, demonstrating
that the bidirectional loop terminates with every eligible entity synced and the
dependency order held (a product only syncs after its variation).

    python -m stone_pipeline.ledger.simulate     # drive the per-env ledger to synced

No em dashes (design principle 2).
"""

from __future__ import annotations

from typing import Callable

from stone_pipeline.ledger import sync
from stone_pipeline.ledger.db import Ledger

_TYPES = ("variations", "products", "inventory")


def simulate_sync(ledger: Ledger, mint: Callable[[str, str], str] | None = None,
                  max_rounds: int = 50) -> dict:
    """Drive the ledger to convergence the way Medusa's pull job would. Each round
    pulls every type's ready batch, 'applies' it (mints a placeholder id), and acks it
    back. A product becomes ready only after its variation is acked, so convergence
    takes as many rounds as the dependency depth. Returns a small report."""
    mint = mint or (lambda type_, external_id: f"SIM-{type_[:3].upper()}-{external_id}")
    applied = {t: 0 for t in _TYPES}
    rounds = 0
    while rounds < max_rounds:
        rounds += 1
        moved = 0
        for type_ in _TYPES:
            for item in sync.ready(ledger, type_):
                sync.ack(ledger, type_, item["external_id"],
                         mint(type_, item["external_id"]), "created")
                applied[type_] += 1
                moved += 1
        if moved == 0:
            break
    # count_ready is the NON-leasing peek: ready() would mark rows 'syncing' as a side effect and
    # falsely report "not converged" next call. Convergence = nothing left eligible to serve.
    converged = all(sync.count_ready(ledger, t) == 0 for t in _TYPES)
    return {"rounds": rounds, "applied": applied, "converged": converged}


def main(argv: list[str] | None = None) -> int:
    import json

    from stone_pipeline.ledger import writethrough

    path = writethrough.ledger_path()
    if not path.exists():
        print(f"no ledger at {path} (run a build with SCRAPER_LEDGER_WRITETHROUGH=1 first)")
        return 1
    with Ledger.open(path, env=writethrough.ENV_NAME) as ledger:
        before = sync.status(ledger)
        report = simulate_sync(ledger)
        after = sync.status(ledger)
    print(json.dumps({"report": report, "status_before": before, "status_after": after},
                     indent=2, sort_keys=True))
    return 0 if report["converged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
