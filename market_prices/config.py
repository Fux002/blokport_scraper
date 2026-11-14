"""Every tunable of the competitor-price path, in one block."""

from pathlib import Path

CONTRACT_VERSION = "3"

SITES_DIR = Path(__file__).resolve().parent / "sites"
LAYOUTS_DIR = Path(__file__).resolve().parent / "sheet_layouts"
VOCABULARY_DIR = Path(__file__).resolve().parent / "vocabulary"
# The one brand-neutral country table a stated origin is read through (market_prices/specs.py, CONTRACT.md "Specs").
COUNTRIES_FILE = Path(__file__).resolve().parent / "countries.yaml"
# Each run's folder is written here first (<RUNS_DIR>/<env>/<brand>/<run_id>), then published; gitignored (/outputs/).
RUNS_DIR = Path(__file__).resolve().parents[1] / "outputs" / "market_prices"
OBSERVATIONS_FILE = "observations.csv"
SITE_STATUS_FILE = "site_status.csv"
LATEST_FILE = "latest.json"
S3_BRAND_PREFIX = "{env_segment}/market_prices/{brand}"
# The run input naming the brand a run or intake works on, read through stone_pipeline.core.env (so it is set as
# SCRAPER_MARKET_PRICES_BRAND, the env module's legacy prefix read too). Required, never the deployment's own brand: one
# deployment's ad-hoc task runs any brand's register (the one shared runner), its keys under that brand's prefix.
RUN_BRAND_ENV = "MARKET_PRICES_BRAND"

# An honest, generic client name: this path never disguises itself as a browser.
ROBOTS_TOKEN = "market-price-observer"
USER_AGENT = f"{ROBOTS_TOKEN}/1.0"
ROBOTS_PATH = "/robots.txt"
MIN_REQUEST_INTERVAL_SECONDS = 2.0
MAX_CRAWL_DELAY_SECONDS = 60.0
# The live transport (market_prices/transport.py): one plain GET per request, no retry. A page of WOO_PAGE_SIZE products
# is a few MB, so a body over the cap is not a catalogue page; it is refused before or while it is read, never kept.
FETCH_TIMEOUT_SECONDS = 30.0
# ... and the whole request, redirects and body together, ends by this wall-clock deadline (FETCH_TIMEOUT_SECONDS bounds
# each read, so a body trickled in just inside it would otherwise hold the request until MAX_RESPONSE_BYTES)
FETCH_DEADLINE_SECONDS = 120.0
MAX_RESPONSE_BYTES = 20 * 1024 * 1024

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
# A "Label: value" item of a description's HTML list is a stated spec (Ceralith states about half its products this
# way). A longer label or value is a sentence that happens to hold a colon, not a spec.
DESCRIPTION_SPEC_MAX_LABEL_CHARS = 60
DESCRIPTION_SPEC_MAX_VALUE_CHARS = 200

# The generic retail collector (collectors/schema_org_product.py): each product page's schema.org Product/Offer data,
# reached through the entry points the site's register row names. The caps bound one site's run; reaching one is said
# in site_status, never passed over silently.
SCHEMA_ORG_MAX_SITEMAPS = 50
# an entry sitemap may be an index, and its sitemaps may be indexes once more; deeper is refused
SCHEMA_ORG_MAX_SITEMAP_DEPTH = 2
SCHEMA_ORG_MAX_LISTING_PAGES = 100
SCHEMA_ORG_MAX_PRODUCT_PAGES = 2000
# the sitemap protocol's own limit for one uncompressed file
SCHEMA_ORG_MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
# how deep the structured data is walked for a Product; deeper nodes are not read
SCHEMA_ORG_MAX_NESTING = 32
# site_status detail names the most common reasons listings were skipped, the rest summed
MALFORMED_REASONS_SHOWN = 3
# The generic declared-HTML collector (collectors/declared_html.py) walks the same entry points under the same caps. A
# page parsed into more elements than this is not read (the response itself is already capped by MAX_RESPONSE_BYTES).
DECLARED_HTML_MAX_ELEMENTS = 200_000
# Mas's sale rule (14 Nov): when a record's price element holds struck text, stated_specs carries this label and value
SALE_FLAG_LABEL = "On sale"
SALE_FLAG_VALUE = "yes"

# Supplier prices read from the product scraper's own fetch (collectors/supplier_scrape.py; the produce publishes them,
# market_prices/supplier.py). Only a declared currency symbol names a currency: another leaves the listing unpriced and
# is counted, never guessed. A price that states one of these words is on request.
CURRENCY_SYMBOLS = {"US$": "USD"}
ON_REQUEST_TEXTS = ("call for price",)
# A supplier scraper's separate price pass, in its run folder (scrapers/base.py write_price_pass, whose PRICES_CSV and
# PRICES_MARKER these equal): its rows, its marker with the statement of whether it read prices ("ok" or why not), and
# the UTC stamp the run folder is named by. The products.csv beside them is never read here.
SCRAPE_ROWS_FILE = "prices.csv"
SCRAPE_MARKER_FILE = "prices_complete.json"
SCRAPE_PRICES_KEY = "prices"
SCRAPE_STAMP_FORMAT = "%Y%m%d_%H%M%S"

# The dev pilot (market_prices/pilot.py): the brand register's own pilot_site, run against dev's bucket, then a summary
# with the first rows. It refuses a pilot site the register has not approved, and any environment but development.
PILOT_SAMPLE_ROWS = 5

# The sheet intake (market_prices/intake.py): the owner's competitor price sheets, from
# <env>/market_prices/<brand>/intake/ on S3, one delivered run per absorbed file. Each file is absorbed once, by its
# sha256, recorded in ABSORBED_FILE under the brand prefix.
INTAKE_DIR_NAME = "intake"
ABSORBED_FILE = "intake_absorbed.csv"
INTAKE_RUNS_DIR_NAME = "intake"
SHEET_COLLECTOR_VERSION = "sheet-intake/1"
# A sheet row's site_status decision: the owner supplied the file, so no terms gate applies.
FILE_DECISION = "supplied"
# Caps on a hostile file: the bytes as uploaded, what an xlsx (a zip) expands to before it is parsed, and the grid.
MAX_INTAKE_FILE_BYTES = 20 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_SHEET_ROWS = 50_000
MAX_SHEET_COLUMNS = 200
# Excel's own limit on a worksheet title. A longer one only comes from a hand-made file, and it would be quoted into
# every row's source_url, so such a file is refused before it is read (sheet_files).
MAX_SHEET_TITLE_CHARS = 31
SHEET_MAX_DIGITS = 18
# A file no layout reads is refused with its first rows quoted, so the layout for it can be written from the message.
INTAKE_REFUSAL_ROWS_SHOWN = 3
