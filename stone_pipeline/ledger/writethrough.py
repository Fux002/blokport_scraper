"""Phase 2 write-through: a real run also populates the ledger (flag-gated, shadow).

OFF by default. Enable with BLOKPORT_LEDGER_WRITETHROUGH=1. When on, run_source
records its emitted products and the inventory delta into the per-env ledger AFTER
the CSVs are written, so the ledger is a shadow mirror and the live CSV flow is
unchanged. A ledger error is caught and logged, never failing the run.

Scope of this phase (kept FK-safe and simple): full runs record products + changed
inventory. Discontinued (cross-run, references products not in this run's emit),
inventory-only refreshes, and seeding the full product set from products_export
all need the product bootstrap (design 5B / M1) and are deliberately left for the
next step. No em dashes (design principle 2).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from stone_pipeline.config.settings import ENV_NAME, SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger import bootstrap, populate
from stone_pipeline.ledger.db import Ledger

log = logfmt.get_logger("ledger.writethrough")

_TRUE = {"1", "true", "yes", "on"}


def enabled() -> bool:
    return os.environ.get("BLOKPORT_LEDGER_WRITETHROUGH", "").strip().lower() in _TRUE


def ledger_path() -> Path:
    """Per-env ledger location. BLOKPORT_LEDGER_PATH overrides it (tests, ops). The
    default sits on local disk (never EFS, design section 12 / M4)."""
    override = os.environ.get("BLOKPORT_LEDGER_PATH", "").strip()
    if override:
        return Path(override)
    return SETTINGS.paths.workspace_root / "ledger" / f"{ENV_NAME}.db"


def open_ledger(path: str | Path | None = None) -> Ledger:
    """Open the per-env ledger, seeding the id foundation (attributes + variations
    from the from_medusa exports) on first use so product FKs resolve. Idempotent:
    the seed runs only when the variation table is empty."""
    p = Path(path or ledger_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger.open(p, env=ENV_NAME)
    if ledger.execute("SELECT COUNT(*) AS n FROM variation").fetchone()["n"] == 0:
        bootstrap.seed_attributes(ledger)
        bootstrap.seed_variations(ledger)
    return ledger


def record_source(emit_rows: Sequence[CanonicalRow], changed: Sequence[CanonicalRow],
                  cfg: SourceConfig, *, inventory_only: bool = False,
                  path: str | Path | None = None) -> None:
    """Shadow-record one source's emitted products + inventory delta into the ledger.
    No-op when the flag is off or in inventory-only mode; never raises (a shadow
    failure must not fail a run)."""
    if not enabled() or inventory_only:
        return
    try:
        with open_ledger(path) as ledger:
            n_products = populate.populate_products(ledger, emit_rows, cfg)
            n_changed = populate.populate_inventory(ledger, changed, cfg)
        log.info("ledger write-through recorded source", extra={"extra_fields": {
            "source": cfg.source_code, "products": n_products, "inventory_changed": n_changed}})
    except Exception:
        log.exception("ledger write-through failed (shadow only; run unaffected)",
                      extra={"extra_fields": {"source": cfg.source_code}})
