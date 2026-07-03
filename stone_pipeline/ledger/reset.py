"""Clean-start reset of the ledger SYNC state, for when Medusa has been wiped and the scraper ledger
must follow. It returns every entity to 'pending' and drops all Medusa ids + sync bookkeeping, so the
next pull re-syncs from scratch with fresh ids. Scraped CONTENT (names, types, images, prices) is
untouched -- only the sync overlay is cleared.

    python -m stone_pipeline.ledger.reset            # dry run: show what WOULD reset
    python -m stone_pipeline.ledger.reset --yes       # actually reset

COORDINATE it: pause Medusa's pull job first, reset here, have Medusa clear its own scraper_sync_ref,
then resume -- otherwise an in-flight ack races the reset. No em dashes in output (design principle 2).
"""

from __future__ import annotations

import json
import sys

from stone_pipeline.ledger import sync, writethrough
from stone_pipeline.ledger.db import Ledger


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    path = writethrough.ledger_path()
    if not path.exists():
        print(f"no ledger at {path}")
        return 1

    try:
        with Ledger.open(path, env=writethrough.ENV_NAME) as ledger:
            before = sync.status(ledger)
            if "--yes" not in argv:
                print(f"DRY RUN (no changes). Ledger: {path}")
                print(json.dumps({"current_status": before}, indent=2, sort_keys=True))
                print("\nre-run with --yes to reset every entity to 'pending' and drop all Medusa ids.")
                print("COORDINATE FIRST: pause Medusa's pull, then reset, then have Medusa clear scraper_sync_ref.")
                return 0
            result = sync.reset_sync_state(ledger)
            after = sync.status(ledger)
    except sync.ServeInFlight as exc:
        print(f"refused: {exc}. Pause Medusa's pull first, then retry.")
        return 2

    print(json.dumps({"reset_rows": result, "status_before": before, "status_after": after},
                     indent=2, sort_keys=True))
    print("\nclean start done. Every entity is 'pending' with no Medusa id; the next pull re-syncs fresh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
