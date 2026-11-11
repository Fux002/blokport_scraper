"""Every tunable of the competitor-price path, in one block."""

from pathlib import Path

CONTRACT_VERSION = "1"

SITES_DIR = Path(__file__).resolve().parent / "sites"
OBSERVATIONS_FILE = "observations.csv"
SITE_STATUS_FILE = "site_status.csv"
LATEST_FILE = "latest.json"
S3_BRAND_PREFIX = "{env_segment}/market_prices/{brand}"

# An honest, generic client name: this path never disguises itself as a browser.
ROBOTS_TOKEN = "market-price-observer"
USER_AGENT = f"{ROBOTS_TOKEN}/1.0"
ROBOTS_PATH = "/robots.txt"
MIN_REQUEST_INTERVAL_SECONDS = 2.0
MAX_CRAWL_DELAY_SECONDS = 60.0

BLOCK_STATUSES = frozenset({401, 403, 406, 429, 451})
MISSING_ROBOTS_STATUSES = frozenset({404, 410})
CHALLENGE_HEADER = "cf-mitigated"
# Phrases of a challenge or firewall page, never single words: an ordinary page can load reCAPTCHA or name a plugin.
CHALLENGE_MARKERS = ("just a moment", "attention required", "verify you are human", "access denied",
                     "has been limited", "website firewall")
CHALLENGE_SCAN_CHARS = 4096
# RFC 9309 asks a parser to read at least 500 KiB; a larger file is refused rather than read in part.
MAX_ROBOTS_CHARS = 512 * 1024

# Under the csv module's 131072 field limit, which the manifest's row count reads through.
MAX_CELL_CHARS = 100_000

WOO_STORE_PATH = "/wp-json/wc/store/v1/products"
WOO_PAGE_SIZE = 100
WOO_MAX_PAGES = 200
WOO_TOTAL_PAGES_HEADER = "x-wp-totalpages"
WOO_MAX_MINOR_UNIT = 4
WOO_MAX_DIGITS = 18
