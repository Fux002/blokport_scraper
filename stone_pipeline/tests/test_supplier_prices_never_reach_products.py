"""A supplier's list price is never the buy price, and the scraper sends BlokPort no price on the product path. Once
Polonine's login fills price1/price2/currency in its scrape, nothing between that scrape and a served product may read
them: the adapter maps no price, the canonical row holds no price field, and no ledger table (whose rows the /sync/v1
payloads are built from) has a price column. Prices leave only through the price pass's own files (supplier_prices, a
separate contract)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from stone_pipeline.adapters.polonine import ADAPTER
from stone_pipeline.core.schema import CanonicalRow

_PRICED = "US$ 59.00<span style='font-size: smaller;'>/Sq.Mt.</span>"


def _priced_scrape_row() -> dict:
    return {"product_id": "101", "material": "ALPINE", "stone_type": "Dolomite Marble", "color": "White",
            "finish": "Polished", "quality": "Standard", "thickness": "2CM", "slab_count": "6",
            "area_sqmt": "55.160", "image_urls": "https://polonine.slabware.com/a.jpg",
            "price1": _PRICED, "price2": "US$ 5.48<span>/Sq.Ft.</span>", "currency": "US$",
            "detail_url": "https://polonine.slabware.com/Product-Details.aspx?ID=101"}


def test_a_filled_price_never_reaches_the_canonical_row():
    row = ADAPTER.adapt_record(_priced_scrape_row())
    dumped = json.dumps(row.model_dump(), default=str)
    assert "59.00" not in dumped and "5.48" not in dumped and "US$" not in dumped


def test_the_canonical_row_has_no_price_field():
    assert [name for name in CanonicalRow.model_fields if "price" in name or "currency" in name] == []


def test_no_ledger_table_has_a_price_column():
    schema = Path(__file__).resolve().parents[1] / "ledger" / "schema.sql"
    db = sqlite3.connect(":memory:")
    db.executescript(schema.read_text(encoding="utf-8"))
    tables = [name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    priced = [(table, column) for table in tables for (_, column, *_) in db.execute(f'PRAGMA table_info("{table}")')
              if "price" in column or "currency" in column]
    assert tables and priced == []
