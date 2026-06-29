"""Phase 2 write-through: a real run also populates the ledger (flag-gated, shadow).

OFF by default. Enable with BLOKPORT_LEDGER_WRITETHROUGH=1. When on, run_source
records its emitted products and the inventory delta into the per-env ledger AFTER
the CSVs are written, so the ledger is a shadow mirror and the live CSV flow is
unchanged. A ledger error is caught and logged, never failing the run.

A run records its emitted products, the changed-stock inventory delta, and the
discontinued delist. The id foundation plus the known product set are seeded on
first use, so inventory and discontinued FKs resolve even for products not in this
run's emit. The inventory and discontinued lanes are dormant until products_export
exists (no known products to diff against). No em dashes (design principle 2).
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
        bootstrap.seed_products(ledger)   # dormant unless products_export exists
    return ledger


def record_source(emit_rows: Sequence[CanonicalRow], changed: Sequence[CanonicalRow],
                  discontinued: Sequence[tuple[str, str]], cfg: SourceConfig, *,
                  path: str | Path | None = None) -> None:
    """Shadow-record one source's emitted products, changed inventory, and discontinued
    delist into the ledger. No-op when the flag is off; never raises (a shadow failure
    must not fail a run). Recorded the same in full and inventory-only runs: the
    emitted rows are valid products in both."""
    if not enabled():
        return
    try:
        with open_ledger(path) as ledger:
            n_products = populate.populate_products(ledger, emit_rows, cfg)
            n_changed = populate.populate_inventory(ledger, changed, cfg)
            n_gone = populate.populate_discontinued(ledger, discontinued)
        log.info("ledger write-through recorded source", extra={"extra_fields": {
            "source": cfg.source_code, "products": n_products,
            "inventory_changed": n_changed, "discontinued": n_gone}})
    except Exception:
        log.exception("ledger write-through failed (shadow only; run unaffected)",
                      extra={"extra_fields": {"source": cfg.source_code}})


def record_catalog(path: str | Path | None = None) -> None:
    """Shadow-record the consolidated catalog: reflect the produced 1_variants_full
    onto the variation table (mark the produced set, add new variants). Call after
    catalog finalizes the file. No-op when the flag is off; never raises."""
    if not enabled():
        return
    try:
        full = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
        if not full.exists():
            return
        with open_ledger(path) as ledger:
            n = populate.populate_variations_full(ledger, full)
        log.info("ledger write-through recorded catalog variations",
                 extra={"extra_fields": {"variants_full": n}})
    except Exception:
        log.exception("ledger catalog write-through failed (shadow only; run unaffected)")
