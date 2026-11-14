"""Every name and value of the supplier_prices contract, and the price reading rules."""

from pathlib import Path

CONTRACT = "supplier_prices"
CONTRACT_VERSION = "1"
PRICES_FILE = "prices.csv"
COLUMNS = ("contract_version", "run_id", "brand", "vendor_key", "vendor_sku", "listing_text", "price_text",
           "price_amount", "price_status", "currency", "vat_basis", "unit_text", "observed_at", "source_url",
           "provenance")

VAT_BASES = frozenset({"incl", "excl"})
PRICED = "priced"
ON_REQUEST = "on_request"

# A price that states one of these (case-folded) is on request.
ON_REQUEST_TEXTS = frozenset({"call for price"})
# Only a declared symbol names a currency; any other leaves the row priced with no amount, so the backend refuses it.
CURRENCY_SYMBOLS = {"US$": "USD"}
# Why a priced row has no amount (provenance.note): a reading defect stays visible to the loader.
NOTE_NO_PRICE_SHOWN = "no price shown"
NOTE_PRICE_NOT_READ = "price not read"
NOTE_UNDECLARED_SYMBOL = "currency symbol not declared"

# A scraper run folder is named by its UTC start (scrapers.base); the run id and observed_at are that moment.
RUN_ID_FORMAT = "%Y%m%dT%H%M%SZ"
OBSERVED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

KEY_PREFIX = "{env_segment}/supplier_prices/{brand}"
RUNS_DIR = Path(__file__).resolve().parents[1] / "outputs" / "supplier_prices"
