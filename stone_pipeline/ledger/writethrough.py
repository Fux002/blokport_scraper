"""Write-through: a real run also populates the ledger, the live sync source Medusa pulls (flag-gated).

OFF by default. Enable with SCRAPER_LEDGER_WRITETHROUGH=1 (legacy BLOKPORT_ still read). When on, run_source
records its emitted products and the inventory delta into the per-env ledger AFTER
the CSVs are written. A ledger error is logged and reported as a failed write-through (record_source /
record_catalog return False), and the caller fails the run on it: a change that did not reach the ledger
never reaches Medusa.

A run records its emitted products, the changed-stock inventory delta, and the
discontinued delist; a stock-only run records the stock and the delist but never a
product (record_source). The id foundation plus the known product set are seeded on
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


def enabled() -> bool:
    """The single predicate for 'is ledger write-through on?' -- every gate calls this, never re-reads env."""
    return env.env_bool("LEDGER_WRITETHROUGH")


def ledger_path() -> Path:
    """Per-env ledger location. BLOKPORT_LEDGER_PATH overrides it (tests, ops). The
    default sits on local disk (never EFS, design section 12 / M4)."""
    override = env.getenv("BLOKPORT_LEDGER_PATH", "").strip()
    if override:
        return Path(override)
    return SETTINGS.paths.workspace_root / "ledger" / f"{ENV_NAME}.db"


def backend_fingerprint() -> str:
    """A stable per-DEPLOYMENT identity (brand + env) stamped into the ledger's ledger_meta, so a wrong-brand
    snapshot restored onto this task fails loud on open instead of being silently accepted. Deterministic
    within a deployment (brand + env are fixed at startup), so re-opens always match; a different brand's
    ledger has a different fingerprint and is rejected (ledger.db _bind_env)."""
    import hashlib
    from stone_pipeline.config.settings import BRAND
    return hashlib.sha256(f"{BRAND}|{ENV_NAME}".encode()).hexdigest()[:16]


def open_ledger(path: str | Path | None = None) -> Ledger:
    """Open the per-env ledger, seeding the id foundation (attributes + variations
    from the from_medusa exports) on first use so product FKs resolve. Idempotent:
    the seed runs only when the variation table is empty."""
    p = Path(path or ledger_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger = Ledger.open(p, env=ENV_NAME, backend_id_fingerprint=backend_fingerprint())
    if ledger.execute("SELECT COUNT(*) AS n FROM variation").fetchone()["n"] == 0:
        bootstrap.seed_attributes(ledger)
        bootstrap.seed_variations(ledger)
        bootstrap.seed_products(ledger)   # dormant unless products_export exists
    return ledger


def mirror_restrictions(ledger: Ledger, *, export_path: str | Path | None = None,
                        attributes_path: str | Path | None = None) -> tuple[int, int]:
    """Mirror the Medusa export's VE2 freeze flags onto the ledger (design section 6): variation.archived from
    the variants export's status, attribute.restricted from the attributes export's status/internal (archived OR
    internal). A PURE FUNCTION of the files, recomputed every produce -- a value no longer archived flips back to
    0 -- and it touches ONLY the rows whose flag actually changes, so an unchanged row keeps its updated_at and is
    never re-served. Dormant when a file is absent (returns 0). Reads through the one reader with the download-path
    status decision, so a file without the column reads tolerant until step C flips the flags. Returns
    (variations changed, attributes changed). Idempotent: a second call with the same files changes nothing."""
    from stone_pipeline.ledger.db import now_iso
    from stone_pipeline.reference.loaders import attributes_status_required, variants_status_required
    from stone_pipeline.reference.medusa_files import read_attributes, read_variants_export

    now = now_iso()
    n_var = 0
    export = Path(export_path) if export_path is not None else SETTINGS.paths.export_file
    if export.exists():
        archived = {v.key for v in read_variants_export(export, require_status=variants_status_required(export))
                    if v.archived}
        current = {r["key"] for r in ledger.execute("SELECT key FROM variation WHERE archived = 1")}
        for key in sorted(archived - current):   # raise only the rows flipping up (a key with no row no-ops)
            n_var += ledger.execute(
                "UPDATE variation SET archived = 1, updated_at = ? WHERE key = ? AND archived = 0",
                (now, key)).rowcount
        for key in sorted(current - archived):   # lower the rows no longer archived in the file
            n_var += ledger.execute(
                "UPDATE variation SET archived = 0, updated_at = ? WHERE key = ?", (now, key)).rowcount

    n_attr = 0
    attrs = Path(attributes_path) if attributes_path is not None else SETTINGS.paths.attributes_csv
    if attrs.exists():
        restricted = {(a.category, a.value) for a in
                      read_attributes(attrs, require_status=attributes_status_required(attrs)) if not a.assignable}
        current = {(r["category"], r["value"]) for r in
                   ledger.execute("SELECT category, value FROM attribute WHERE restricted = 1")}
        for category, value in sorted(restricted - current):
            n_attr += ledger.execute(
                "UPDATE attribute SET restricted = 1, updated_at = ? "
                "WHERE category = ? AND value = ? AND restricted = 0", (now, category, value)).rowcount
        for category, value in sorted(current - restricted):
            n_attr += ledger.execute(
                "UPDATE attribute SET restricted = 0, updated_at = ? WHERE category = ? AND value = ?",
                (now, category, value)).rowcount
    return n_var, n_attr


def record_source(emit_rows: Sequence[CanonicalRow],
                  discontinued: Sequence[tuple[str, str]], cfg: SourceConfig, *, inventory_only: bool,
                  replaced: dict[str, str] | None = None, path: str | Path | None = None) -> bool:
    """Record one source's emitted products, their stock, and the discontinued delist into the ledger.
    No-op (returns True) when the flag is off. Never RAISES, but returns False if the write failed so the
    caller can surface it -- the ledger is the live sync source, so a swallowed failure would silently drop
    a catalog/stock change. `replaced` (sku -> lot_code): discontinued bundles that were replaced by their lot.

    `inventory_only` (the stock-only refresh) records stock only. That run skips the image stage, so its
    rows carry no photos, gallery or 3D: written as products they would strip every product's images and
    re-serve it without them. So it never writes a product row; it moves the stock of the products the
    ledger already holds, and leaves a product new to the ledger (and its stock) to the full run that
    creates it with its images. The delist and the withdrawal of unsent products run the same in both modes.
    The mode is required, so no caller can record a stock-only run as a full one by omission.

    Inventory is seeded from the FULL emit set (every product's current stock), not a
    pre-computed delta. The ledger tracks `last_synced_qty` per sku, so it derives the
    true deltas itself: a never-synced row is a delta, an unchanged synced row is not. This
    is what makes a first/baseline-less load (no products_export) carry initial stock -- the
    old delta-only feed left the lane empty there, so Medusa loaded every product at qty 0."""
    if not enabled():
        return True
    try:
        with open_ledger(path) as ledger:
            if inventory_only:
                n_products = 0
                stock_rows = populate.ledger_products(ledger, emit_rows, cfg)
            else:
                n_products = populate.populate_products(ledger, emit_rows, cfg)
                stock_rows = emit_rows
            n_stock = populate.populate_inventory(ledger, stock_rows, cfg)
            n_withdrawn = populate.withdraw_unsent(ledger, emit_rows, cfg)
            n_gone = populate.populate_discontinued(ledger, discontinued, replaced)
        log.info("ledger write-through recorded source", extra={"extra_fields": {
            "source": cfg.source_code, "inventory_only": inventory_only, "products": n_products,
            "inventory": n_stock, "left_to_full_run": len(emit_rows) - len(stock_rows),
            "withdrawn_unsent": n_withdrawn, "discontinued": n_gone}})
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
            # VE2: mirror the export's freeze flags once per produce, after the variation rows are in place
            # (so a fresh ledger is bootstrapped first). Reads the from_medusa files the produce just fetched.
            archived_changed, restricted_changed = mirror_restrictions(ledger)
        log.info("ledger write-through recorded catalog variations",
                 extra={"extra_fields": {"variants_full": n, "types_filled": typed,
                                         "archived_changed": archived_changed,
                                         "restricted_changed": restricted_changed}})
        return True
    except Exception:
        log.exception("ledger catalog write-through FAILED: produced variations did NOT reach the ledger; "
                      "Medusa will not sync them until a successful re-run")
        return False
