"""What each source has IN the scraper, for the :4200 list -- so it shows what's there and how old,
not just the last run. Reports the latest raw scrape (when + how many rows) and how many of the
source's products reached the ledger. Filesystem + ledger reads only; no config DB.
"""

from __future__ import annotations

from datetime import datetime

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import logfmt

log = logfmt.get_logger("config.scrape_status")


def scrape_info(source: str) -> dict:
    """The latest raw scrape for `source` at data/<source>/<YYYYMMDD_HHMMSS>/products.csv: when it was
    scraped and how many rows. Nulls if the source has never been scraped."""
    files = sorted(SETTINGS.paths.data_dir.glob(f"{source}/*/products.csv"), reverse=True)
    if not files:
        return {"scrape_at": None, "scrape_rows": None}
    f = files[0]
    try:
        scrape_at = datetime.strptime(f.parent.name, "%Y%m%d_%H%M%S").isoformat()  # the scrape's wall clock
    except ValueError:
        scrape_at = None
    try:
        with f.open(encoding="utf-8-sig") as handle:
            rows = max(sum(1 for _ in handle) - 1, 0)   # minus the header row
    except OSError:
        rows = None
    return {"scrape_at": scrape_at, "scrape_rows": rows}


def ledger_product_counts() -> dict[str, int]:
    """{source_code: produced-product count} from the ledger. Empty on any error (best-effort)."""
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger
    try:
        with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
            return {r["source"]: r["n"] for r in
                    lg.execute("SELECT source, COUNT(*) AS n FROM product GROUP BY source")}
    except Exception:
        log.exception("failed to read ledger product counts (non-fatal)")
        return {}


def enrich(rows: list[dict]) -> list[dict]:
    """Add scrape_at / scrape_rows (the raw scrape) and ledger_products (what's produced) to each
    source row, so the admin list can show what's in the scraper and how old it is."""
    counts = ledger_product_counts()
    for r in rows:
        r.update(scrape_info(r["source"]))
        r["ledger_products"] = counts.get(r.get("source_code"), 0)
    return rows
