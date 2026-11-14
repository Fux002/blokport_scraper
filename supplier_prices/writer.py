"""One vendor's price pass (its scraper run folder's prices.csv and marker) written as one supplier_prices run folder:
<root>/<brand>/<run_id>/prices.csv, in the contract's columns. Generic: the source's own adapter says which column holds
the price and builds the key the product emit's SKU is made from, so every row names the variant that source serves."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from market_prices.text import clean_text
from scrapers.base import PRICES_CSV, PRICES_KEY, PRICES_MARKER, RUN_STAMP_FORMAT
from stone_pipeline import adapters
from stone_pipeline.config.sources import load_source
from stone_pipeline.core.logfmt import get_logger
from stone_pipeline.stages.keys_dedupe import natural_surrogate
from stone_pipeline.stages.product_state import sku_from
from supplier_prices import config

log = get_logger("supplier_prices")

# A symbol, then one amount (plain, or grouped by commas in threes, a dot as its decimal mark), then "/" and the unit.
_PRICE = re.compile(r"^(?P<symbol>[^\d\s]+)\s*(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
                    r"(?:\s*/\s*(?P<unit>\S.*))?$")


class NoPrices(RuntimeError):
    """The price pass states it read no prices (a refused login, say): nothing was seen, so there is no run."""


@dataclass(frozen=True)
class RunWritten:
    source: str
    run_id: str
    run_dir: Path
    vendor: dict
    rows: int
    malformed: int      # rows the source's adapter could not map: dropped, as the product path's adapt drops them
    left_out: int       # rows with no natural key: the product path would mint a key from row order, never guessed here
    duplicates: int     # rows whose SKU an earlier row already holds: the product path keeps the first


@dataclass(frozen=True)
class _Price:
    text: str
    status: str = config.PRICED
    amount: str = ""
    currency: str = ""
    unit: str = ""
    note: Optional[str] = None


def read_price(stated: object) -> _Price:
    """The price as shown: on request, or priced with its exact amount, declared currency and unit. A price that cannot
    be read stays priced with no amount and the reason, so the loader refuses that row by name."""
    text = clean_text(stated)
    if text is None:
        return _Price(text="", note=config.NOTE_NO_PRICE_SHOWN)
    if text.casefold() in config.ON_REQUEST_TEXTS:
        return _Price(text=text, status=config.ON_REQUEST)
    found = _PRICE.match(text)
    amount = found.group("amount").replace(",", "") if found else ""
    if not amount or Decimal(amount) <= 0:
        return _Price(text=text, note=config.NOTE_PRICE_NOT_READ)
    currency = config.CURRENCY_SYMBOLS.get(found.group("symbol"))
    if currency is None:
        return _Price(text=text, note=config.NOTE_UNDECLARED_SYMBOL)
    return _Price(text=text, amount=amount, currency=currency, unit=found.group("unit") or "")


def _pass_rows(folder: Path) -> list[dict]:
    """The pass's rows when it read prices; NoPrices with the marker's reason when it did not."""
    stated = json.loads((folder / PRICES_MARKER).read_text(encoding="utf-8")).get(PRICES_KEY)
    if stated != "ok":
        raise NoPrices(f"{folder.parent.name}'s price pass of {folder.name} read no prices: "
                       f"{stated or 'its marker states nothing'}")
    rows_file = folder / PRICES_CSV
    if not rows_file.is_file():
        return []
    with rows_file.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_run(folder: Path, root: Path, brand: str) -> RunWritten:
    """The run of the price pass in `folder` (<data>/<source>/<stamp>), written under root/<brand>/<run_id>/."""
    source, stamp = folder.parent.name, folder.name
    adapter = adapters.REGISTRY.get(source)
    if adapter is None:
        raise ValueError(f"{source} has no adapter, so its price pass is not read")
    field = adapter.supplier_price_field
    if not field:
        raise ValueError(f"{source}'s adapter declares no supplier_price_field, so its price pass is not read")
    if adapter.supplier_vat_basis not in config.VAT_BASES:
        raise ValueError(f"{source}'s adapter declares supplier_vat_basis {adapter.supplier_vat_basis!r}, "
                         f"not one of {sorted(config.VAT_BASES)}")
    cfg = load_source(source)
    if not cfg.vendor:
        raise ValueError(f"{source} has no vendor configured: a supplier_prices run names its vendor")
    rows = _pass_rows(folder)
    moment = datetime.strptime(stamp, RUN_STAMP_FORMAT).replace(tzinfo=timezone.utc)
    run_id = moment.strftime(config.RUN_ID_FORMAT)
    common = {"contract_version": config.CONTRACT_VERSION, "run_id": run_id, "brand": brand, "vendor_key": source,
              "vat_basis": adapter.supplier_vat_basis, "observed_at": moment.strftime(config.OBSERVED_AT_FORMAT)}
    by_sku: dict[str, dict] = {}
    malformed = left_out = duplicates = 0
    for raw in rows:
        try:
            listing = adapter.adapt_record(raw)
        except Exception as exc:    # broad on purpose, as AdapterBase.adapt: one unforeseen row never costs the run
            malformed += 1
            log.warning(f"{source} price pass {stamp}: the adapter could not map a row: {type(exc).__name__}: {exc}")
            continue
        key = natural_surrogate(listing) if listing is not None else ""
        if not key:
            left_out += 1
            continue
        sku = sku_from(cfg.source_code, key)
        if sku in by_sku:
            duplicates += 1
            continue
        price = read_price(raw.get(field))
        provenance = {"price_field": field, "scrape": stamp, **({"note": price.note} if price.note else {})}
        by_sku[sku] = {**common, "vendor_sku": sku, "listing_text": listing.raw_name or "", "price_text": price.text,
                       "price_amount": price.amount, "price_status": price.status, "currency": price.currency,
                       "unit_text": price.unit, "source_url": listing.src_url or "",
                       "provenance": json.dumps(provenance, sort_keys=True, separators=(",", ":"))}
    if malformed or left_out or duplicates:
        log.warning(f"{source} price pass {stamp}: {malformed} malformed row(s) dropped, {left_out} row(s) with no key "
                    f"left out, {duplicates} repeated SKU(s) kept as the first row")
    run_dir = root / brand / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / config.PRICES_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=config.COLUMNS)
        writer.writeheader()
        writer.writerows(by_sku[sku] for sku in sorted(by_sku))
    return RunWritten(source=source, run_id=run_id, run_dir=run_dir,
                      vendor={"vendor_key": source, "vendor_name": cfg.vendor},
                      rows=len(by_sku), malformed=malformed, left_out=left_out, duplicates=duplicates)
