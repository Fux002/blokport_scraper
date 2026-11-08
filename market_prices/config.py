"""Every tunable of the competitor-price path, in one block."""

from pathlib import Path

CONTRACT_VERSION = "1"

SITES_DIR = Path(__file__).resolve().parent / "sites"
OBSERVATIONS_FILE = "observations.csv"
SITE_STATUS_FILE = "site_status.csv"
LATEST_FILE = "latest.json"
S3_BRAND_PREFIX = "{env_segment}/market_prices/{brand}"

# An honest, generic client name: this path never disguises itself as a browser.
USER_AGENT = "market-price-observer/1.0"

BLOCK_STATUSES = frozenset({401, 403, 429})
MISSING_ROBOTS_STATUSES = frozenset({404, 410})
CHALLENGE_HEADER = "cf-mitigated"
CHALLENGE_MARKERS = ("just a moment", "attention required", "verify you are human", "captcha")
CHALLENGE_SCAN_CHARS = 4096

WOO_STORE_PATH = "/wp-json/wc/store/v1/products"
WOO_PAGE_SIZE = 100
WOO_MAX_PAGES = 200
WOO_TOTAL_PAGES_HEADER = "x-wp-totalpages"
WOO_MAX_MINOR_UNIT = 4
