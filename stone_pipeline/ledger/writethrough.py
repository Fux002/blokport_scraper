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

from stone_pipeline.core import env
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
    return env.getenv("BLOKPORT_LEDGER_WRITETHROUGH", "").strip().lower() in _TRUE


def ledger_path() -> Path:
    """Per-env ledger location. BLOKPORT_LEDGER_PATH overrides it (tests, ops). The
    default sits on local disk (never EFS, design section 12 / M4)."""
    override = env.getenv("BLOKPORT_LEDGER_PATH", "").strip()
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


def record_source(emit_rows: Sequence[CanonicalRow],
                  discontinued: Sequence[tuple[str, str]], cfg: SourceConfig, *,
                  path: str | Path | None = None) -> None:
    """Record one source's emitted products, their stock, and the discontinued delist into the ledger.
    No-op (returns True) when the flag is off. Never RAISES, but returns False if the write failed so the
    caller can surface it -- the ledger is the live sync source, so a swallowed failure would silently drop
    a catalog/stock change. Recorded the same in full and inventory-only runs: the emitted rows are valid
    products in both.

    Inventory is seeded from the FULL emit set (every product's current stock), not a
    pre-computed delta. The ledger tracks `last_synced_qty` per sku, so it derives the
    true deltas itself: a never-synced row is a delta, an unchanged synced row is not. This
    is what makes a first/baseline-less load (no products_export) carry initial stock -- the
    old delta-only feed left the lane empty there, so Medusa loaded every product at qty 0."""
    if not enabled():
        return True
    try:
        with open_ledger(path) as ledger:
            n_products = populate.populate_products(ledger, emit_rows, cfg)
            n_stock = populate.populate_inventory(ledger, emit_rows, cfg)
            n_gone = populate.populate_discontinued(ledger, discontinued)
        log.info("ledger write-through recorded source", extra={"extra_fields": {
            "source": cfg.source_code, "products": n_products,
            "inventory": n_stock, "discontinued": n_gone}})
        return True
    except Exception:
        # NOT "shadow only" anymore: the ledger IS the live sync source (Medusa pulls it). A failure here
        # means this source's products/stock/delist did NOT reach the ledger, so Medusa never gets them --
        # the run still finishes (the CSVs are written) but the caller MUST surface it, not report a clean
        # success, or a stock/catalog change silently fails to propagate until a later run happens to work.
        log.exception("ledger write-through FAILED: source did not reach the ledger; Medusa will not "
                      "receive it until a successful re-run", extra={"extra_fields": {"source": cfg.source_code}})
        return False


def record_catalog(path: str | Path | None = None) -> bool:
    """Record the consolidated catalog: reflect the produced 1_variants_full onto the variation table (mark
    the produced set, add new variants). Call after catalog finalizes the file. Returns True on success or a
    no-op (flag off / file not built yet); returns False if the write FAILED. Never raises, but the failure
    is a status the caller MUST surface: the ledger is the live sync source Medusa pulls, so a swallowed
    failure silently keeps the produced variations OUT of Medusa until a later run happens to succeed."""
    if not enabled():
        return True
    try:
        full = SETTINGS.paths.to_upload_dir / "1_variants_full.csv"
        if not full.exists():
            return True
        with open_ledger(path) as ledger:
            n = populate.populate_variations_full(ledger, full)
            typed = populate.fill_variation_types(ledger)
        log.info("ledger write-through recorded catalog variations",
                 extra={"extra_fields": {"variants_full": n, "types_filled": typed}})
        return True
    except Exception:
        log.exception("ledger catalog write-through FAILED: produced variations did NOT reach the ledger; "
                      "Medusa will not sync them until a successful re-run")
        return False
