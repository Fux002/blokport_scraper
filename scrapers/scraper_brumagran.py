"""
Bruma Granite Inventory Scraper
================================
Same SlabWare SaaS platform as Ferraz Brasil and Zucchi Stones - this is
just another tenant. Architecture is identical: Cloudflare-fronted ASP.NET
WebForms with two key WebMethods (ObterListaBundles, DetalheBundle).

Hits the listing endpoint for the bundle catalog, then DetalheBundle for
each bundle to enrich with classification, color, multi-photo, per-slab
dimensions etc. Downloads every photo from the fotos[] array. Outputs one
CSV row per bundle.

Prices are gated by SlabWare's central permissions block (Logado: false ->
all price fields return a 'Login for Price' HTML stub). Public scrape only.

Install once:
    pip3 install curl_cffi --break-system-packages
"""

from curl_cffi import requests
import json
import csv
import time
import random
import re
import logging
from pathlib import Path
from datetime import datetime

# ============================================================================
# TUNABLE KNOBS - adjust these to control speed and politeness
# ============================================================================

# --- File paths -------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent if '__file__' in globals() else Path.cwd()
IMAGES_DIR = SCRIPT_DIR / "images"
TIMESTAMP_TAG = datetime.now().strftime('%Y%m%d_%H%M%S')
OUTPUT_CSV = SCRIPT_DIR / f"bruma_products_{TIMESTAMP_TAG}.csv"
LOG_FILE = SCRIPT_DIR / f"bruma_scraper_{TIMESTAMP_TAG}.log"
FAILURES_CSV = SCRIPT_DIR / f"bruma_failures_{TIMESTAMP_TAG}.csv"

# --- Site config ------------------------------------------------------------
# The S= token from the URL. SlabWare seems happy to accept empty json filter
# regardless of the token value, but we use a fresh one to be safe.
S_TOKEN = ("9t5AptkpH/x2GD3oStI0wQzByO78puNBq5ZOBU2x8a1DsWdZJ9W+UFgwpAvr7qTXgoGcUrL"
           "nmVFKc1f53xV19kVT86meSR82TYzwo9Agi4tIXRQahJ4warnLVR8jPRR5Nlzhv/4jv1JmwSC9p"
           "dc1AFx8qKbR3xl0HEU9txLHv39MVXNx4b/vifBF0gOe/RM6YI9AB5JLYmXnoGXC6f7T/+S+1d"
           "ulKBuvbEhB+S2IbXo1zOwjkgqK/QVUY3kSlWyk7lwRNLLT83oKvJydG9Zg0bGWjMZfKYA0Qiiu"
           "ey5iIwgYWsUV5CZIcZ77MNZQVm8P8nOTfEHHhIwrCyGNF6ja14E5TfJC7DosXLzzo0/BoVhEqM"
           "1N1IjZnsejo8GLApg0wrNco62bHNqjnUyCm4V1mwVqYp5Yo6gu32pxZdm8yP3p2xPlkCUn1mbu"
           "Fz0MzF2tlEgSNd0mSl7f78CyNIjt4sNkEdc4/JIpu0pHxqVEznBaZo70Jh3pFF7s9jnoa+9V")

BASE = "https://www.brumagraninventory.com"
PAGE_URL = f"{BASE}/FullInventory.aspx?S={S_TOKEN}"
API_URL = f"{BASE}/FullInventory.aspx/ObterListaBundles"
DETAIL_API_URL = f"{BASE}/FullInventory.aspx/DetalheBundle"

# Server-controlled, do not change. Confirmed via probe.
PAGE_SIZE = 40

# --- Speed / politeness -----------------------------------------------------
# Delay BEFORE each listing API call: random.uniform(MIN, MAX) seconds.
# At 40 bundles per call and ~1500 bundles, that's ~38 calls. Conservative.
PAGE_DELAY_MIN = 3.0
PAGE_DELAY_MAX = 6.0

# Delay BEFORE each detail API call. Detail calls are ~1500 of them, so we
# can't be as conservative as the listing calls or the run takes 3+ hours.
# These are still polite given the lighter server-side work per call.
DETAIL_DELAY_MIN = 1.5
DETAIL_DELAY_MAX = 3.5

# Delay BEFORE each image download. Cloudflare also fronts the images here
# (unlike Zucchi where images came from a CloudFront CDN), so be more polite.
IMAGE_DELAY_MIN = 0.6
IMAGE_DELAY_MAX = 1.5

# Every N pages, take a longer "human break" to break up the steady cadence.
LONG_BREAK_EVERY_N_PAGES = 15
LONG_BREAK_MIN = 30.0
LONG_BREAK_MAX = 90.0

# --- Retry / backoff --------------------------------------------------------
MAX_RETRIES = 5
BACKOFF_BASE = 2.0
RATE_LIMIT_BACKOFF = 60.0

# --- HTTP timeouts ----------------------------------------------------------
API_TIMEOUT = 30
IMAGE_TIMEOUT = 30

# --- Browser impersonation --------------------------------------------------
# Chrome TLS fingerprint - curl_cffi rotates the version internally.
# Tested versions: chrome120, chrome119, chrome116, chrome110.
IMPERSONATE = "chrome120"

# --- Image URL pattern ------------------------------------------------------
# Confirmed pattern from a live page inspection. The path embeds the bundle's
# integer id as a folder, then the GUID filename. The site serves two variants:
#   <id>/<guid>.jpg                        - full resolution original
#   <id>/<guid>_crop_555x300.jpg           - thumbnail
# We try full-resolution first and fall back to the thumbnail if it 404s.
IMAGE_PATH_FULL = "/backendGranite/cadastros/Bundles/fotos/{bundle_id}/{filename}"
IMAGE_PATH_THUMB = "/backendGranite/cadastros/Bundles/fotos/{bundle_id}/{stem}_crop_555x300{ext}"

# ============================================================================

# -- Logging -----------------------------------------------------------------

logger = logging.getLogger('bruma')
logger.setLevel(logging.DEBUG)
logger.propagate = False

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(_console_handler)

_file_handler = logging.FileHandler(LOG_FILE, mode='w', encoding='utf-8')
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
))
logger.addHandler(_file_handler)


_failures = []


def record_failure(kind, **details):
    entry = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'kind': kind,
        **details,
    }
    _failures.append(entry)
    logger.warning("FAILURE [%s] %s", kind,
                   ' '.join(f'{k}={v!r}' for k, v in details.items()))


def write_failures_csv():
    if not _failures:
        return
    keys = ['timestamp', 'kind', 'page_inicio', 'bundle_id',
            'url', 'status', 'attempts', 'error']
    with open(FAILURES_CSV, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        writer.writeheader()
        for entry in _failures:
            writer.writerow(entry)


# -- HTTP --------------------------------------------------------------------

session = requests.Session(impersonate=IMPERSONATE)

API_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=utf-8",
    "origin": BASE,
    "referer": PAGE_URL,
    "x-requested-with": "XMLHttpRequest",
}


def _sleep_jitter(low, high, label=None):
    secs = random.uniform(low, high)
    if label:
        logger.info(f"    Waiting {secs:.1f}s ({label})...")
    time.sleep(secs)


def warm_up_session():
    """GET the page once to seed cookies and pass any Cloudflare check."""
    logger.info("Warming up session with GET %s ...", PAGE_URL[:80] + "...")
    r = session.get(PAGE_URL, timeout=30)
    logger.info("  Status: %d, cookies: %s",
                r.status_code, list(session.cookies.keys()))
    if r.status_code != 200:
        raise RuntimeError(f"Warm-up GET failed: {r.status_code}")


def fetch_page(inicio):
    """Call ObterListaBundles for one batch, returns list of bundle dicts."""
    payload = {"inicio": inicio, "json": ""}

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.post(API_URL, headers=API_HEADERS,
                             data=json.dumps(payload), timeout=API_TIMEOUT)

            if r.status_code == 429:
                retry_after = r.headers.get('Retry-After')
                wait = float(retry_after) if retry_after else RATE_LIMIT_BACKOFF
                logger.warning("HTTP 429 on inicio=%d. Sleeping %.0fs...",
                               inicio, wait)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

            j = r.json()
            d = j.get('d') or {}
            bundles_str = d.get('Bundles') or '[]'
            return json.loads(bundles_str)
        except Exception as e:
            last_err = e
            wait = BACKOFF_BASE ** attempt
            logger.warning("inicio=%d attempt %d/%d failed: %s. Waiting %.0fs...",
                           inicio, attempt + 1, MAX_RETRIES, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch inicio={inicio}: {last_err}")


def fetch_detail(bundle_id):
    """Call DetalheBundle for a single bundle, returns the inner Bundle dict.

    Returns None if the call fails after all retries (so the scraper continues
    rather than aborting the whole run on a single bad bundle).
    """
    payload = {"IdBundle": bundle_id, "IdCampanha": 0}

    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            r = session.post(DETAIL_API_URL, headers=API_HEADERS,
                             data=json.dumps(payload), timeout=API_TIMEOUT)

            if r.status_code == 429:
                retry_after = r.headers.get('Retry-After')
                wait = float(retry_after) if retry_after else RATE_LIMIT_BACKOFF
                logger.warning("HTTP 429 on detail bundle=%s. Sleeping %.0fs...",
                               bundle_id, wait)
                time.sleep(wait)
                continue

            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")

            j = r.json()
            d = j.get('d') or {}
            return d.get('Bundle') or {}
        except Exception as e:
            last_err = e
            wait = BACKOFF_BASE ** attempt
            logger.warning("detail bundle=%s attempt %d/%d failed: %s. Waiting %.0fs...",
                           bundle_id, attempt + 1, MAX_RETRIES, e, wait)
            time.sleep(wait)

    record_failure('detail', bundle_id=bundle_id, attempts=MAX_RETRIES,
                   error=str(last_err))
    return None


# -- Image URL construction --------------------------------------------------

def build_image_urls(bundle_id, filename):
    """Return (full_url, thumb_url) for a bundle.

    Both built from the confirmed path pattern. Full-res is tried first by
    the downloader; thumbnail is the fallback if the original isn't on disk.
    """
    if not bundle_id or not filename:
        return ('', '')

    # Split filename into stem + extension for the thumbnail variant
    if '.' in filename:
        stem, dot, ext = filename.rpartition('.')
        ext = '.' + ext
    else:
        stem, ext = filename, '.jpg'

    full = BASE + IMAGE_PATH_FULL.format(bundle_id=bundle_id, filename=filename)
    thumb = BASE + IMAGE_PATH_THUMB.format(bundle_id=bundle_id, stem=stem, ext=ext)
    return (full, thumb)


# -- Parsing -----------------------------------------------------------------

DISPLAY_RE = re.compile(r"class='([^']+)'", re.IGNORECASE)


def parse_display_status(html):
    """The displayProduct field is a tiny HTML span. Extract a clean status."""
    if not html:
        return ''
    m = DISPLAY_RE.search(html)
    if not m:
        return ''
    classes = m.group(1).lower()
    if 'recommended-product' in classes and 'display: block' in html.lower():
        return 'recommended'
    if 'new-product' in classes and 'display: block' in html.lower():
        return 'new'
    return ''


def get(d, key, default=''):
    v = d.get(key)
    if v is None or v == '':
        return default
    return v


# Detect HTML stubs that the API returns for fields the user can't see.
# When unauthenticated, prices are returned as '<a>Login for Price</a>'.
# When the field is genuinely empty, it's an empty string.
_LOGIN_STUB_RE = re.compile(r'<\s*a\s|login\s*for\s*price', re.IGNORECASE)


def clean_price(value):
    """Return '' if the value is an HTML 'Login for Price' stub, else as-is."""
    if not value:
        return ''
    s = str(value)
    if _LOGIN_STUB_RE.search(s):
        return ''
    return s


def join_slabs(chapas, key):
    """Pipe-join a single field across all slabs, e.g. all widths."""
    if not chapas:
        return ''
    return ' | '.join(str(c.get(key, '') or '') for c in chapas)


def join_photos(fotos):
    """Convert array of photo paths to pipe-separated full URLs."""
    if not fotos:
        return ''
    out = []
    for path in fotos:
        if not path:
            continue
        if path.startswith('http'):
            out.append(path)
        elif path.startswith('/'):
            out.append(BASE + path)
        else:
            out.append(BASE + '/' + path)
    return ' | '.join(out)


def build_image_url(filename):
    """Stub kept for compatibility - use build_image_urls(bundle_id, filename) instead."""
    return ''


def parse_bundle(item):
    """Flatten one item from listing Bundles[] into a dict matching CSV_FIELDS.

    Detail-only fields are filled in later by merge_detail() once the
    DetalheBundle call returns. Initialized empty here so CSV writer is happy.
    """
    bundle_id = item.get('id')
    filename = item.get('fotoPrincipal') or ''
    full_url, thumb_url = build_image_urls(bundle_id, filename)
    return {
        # From listing
        'bundle_id': get(item, 'id'),
        'material_name': get(item, 'nomeMaterial'),
        'composition': get(item, 'nomeComposicao'),
        'block': get(item, 'bloco'),
        'bundle_ref': get(item, 'cavalete'),
        'finish': get(item, 'acabamento'),
        'slab_count': get(item, 'qtdChapas'),
        'slabs': get(item, 'chapas'),
        'average_size': get(item, 'averageSize'),
        'quality': get(item, 'nomeQualidade'),
        'thickness': get(item, 'nomeEspessura'),
        'price_1': get(item, 'preco1'),
        'price_old_1': get(item, 'precoAntigo1'),
        'price_2': get(item, 'preco2'),
        'price_old_2': get(item, 'precoAntigo2'),
        'display_status': parse_display_status(item.get('displayProduct', '')),
        'country_code': get(item, 'siglaPais'),
        'country': get(item, 'nomePais'),
        'image_filename_remote': filename,
        'image_url_full': full_url,
        'image_url_thumb': thumb_url,
        # Filled in by merge_detail()
        'classification': '',
        'color': '',
        'location': '',
        'currency': '',
        'description': '',
        'video_url': '',
        'slabs_available': '',
        'total_area_summary': '',
        'avg_size_metric': '',
        'avg_size_imperial': '',
        'total_sqft': '',
        'total_sqmt': '',
        'kitchen_visualizer_url': '',
        'price_main': '',
        'price_main_old': '',
        'price_secondary': '',
        'price_secondary_old': '',
        'price_total_bundle': '',
        'photo_urls': '',
        'photo_count': 0,
        'slab_ids': '',
        'slab_numbers': '',
        'slab_widths_m': '',
        'slab_heights_m': '',
        'slab_lengths_in': '',
        'slab_heights_in': '',
        'slab_areas_sqmt': '',
        'slab_areas_sqft': '',
        'slab_count_actual': '',
    }


def merge_detail(row, detail):
    """Enrich a row with the fields from a DetalheBundle response."""
    if not detail:
        return row

    fotos = detail.get('fotos') or []
    chapas = detail.get('chapas') or []

    row.update({
        'classification': get(detail, 'classificacao'),
        'color': get(detail, 'cor'),
        'location': get(detail, 'localizacao'),
        'currency': get(detail, 'moeda'),
        'description': get(detail, 'observacao'),
        'video_url': get(detail, 'video'),
        'slabs_available': get(detail, 'slabsAvailable'),
        'total_area_summary': get(detail, 'totalValoresMaterialSlabs'),
        'avg_size_metric': get(detail, 'averageSize'),
        'avg_size_imperial': get(detail, 'averageSizeSecundario'),
        'total_sqft': get(detail, 'totalSqft'),
        'total_sqmt': get(detail, 'totalSqmt'),
        'kitchen_visualizer_url': BASE + detail['linkProduto']
            if detail.get('linkProduto', '').startswith('/') else get(detail, 'linkProduto'),
        'price_main': clean_price(detail.get('precoPrincipal')),
        'price_main_old': clean_price(detail.get('precoAntigoPrincipal')),
        'price_secondary': clean_price(detail.get('precoSecundario')),
        'price_secondary_old': clean_price(detail.get('precoAntigoSecundario')),
        'price_total_bundle': clean_price(detail.get('totalFullUs')),
        'photo_urls': join_photos(fotos),
        'photo_count': len(fotos),
        'slab_ids': join_slabs(chapas, 'Id'),
        'slab_numbers': join_slabs(chapas, 'Numero'),
        'slab_widths_m': join_slabs(chapas, 'Largura'),
        'slab_heights_m': join_slabs(chapas, 'Altura'),
        'slab_lengths_in': join_slabs(chapas, 'Length'),
        'slab_heights_in': join_slabs(chapas, 'Height'),
        'slab_areas_sqmt': join_slabs(chapas, 'TotalSqmt'),
        'slab_areas_sqft': join_slabs(chapas, 'TotalSqft'),
        'slab_count_actual': len(chapas),
    })

    # The detail response often has a richer composition string and a populated
    # country code/location that the listing lacked. Override only if non-empty.
    for src_key, dst_key in [('nomeComposicao', 'composition'),
                             ('siglaPais', 'country_code'),
                             ('localizacao', 'location')]:
        v = detail.get(src_key)
        if v:
            row[dst_key] = v

    return row


# -- Image download ----------------------------------------------------------

def clean_id(s):
    return re.sub(r'[<>:"/\\|?*\s]', '', str(s or ''))


def _download_one_image(url, filepath, bundle_id, label):
    """Download one image to filepath. Returns True on success, False on fail."""
    try:
        _sleep_jitter(IMAGE_DELAY_MIN, IMAGE_DELAY_MAX)
        r = session.get(url, timeout=IMAGE_TIMEOUT, headers={
            'Referer': PAGE_URL,
            'accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
        })
        if r.status_code == 429:
            retry_after = r.headers.get('Retry-After')
            wait = float(retry_after) if retry_after else RATE_LIMIT_BACKOFF
            logger.warning("HTTP 429 on image %s. Sleeping %.0fs and retrying once...",
                           filepath.name, wait)
            time.sleep(wait)
            r = session.get(url, timeout=IMAGE_TIMEOUT, headers={
                'Referer': PAGE_URL,
                'accept': 'image/avif,image/webp,image/apng,image/*,*/*;q=0.8',
            })
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        ct = r.headers.get('content-type', '')
        if 'image' not in ct.lower():
            raise RuntimeError(f"Non-image content-type: {ct}")
        with open(filepath, 'wb') as f:
            f.write(r.content)
        logger.debug("Image saved: %s (%d bytes, %s)",
                     filepath.name, len(r.content), label)
        return True
    except Exception as e:
        record_failure(
            'image',
            bundle_id=bundle_id,
            url=url,
            status=getattr(getattr(e, 'response', None), 'status_code', None),
            error=str(e),
        )
        return False


def _thumb_url_from_full(full_url):
    """Convert /...../{stem}{ext} to /...../{stem}_crop_555x300{ext}."""
    if not full_url or '_crop_555x300' in full_url:
        return ''
    if '.' in full_url.rsplit('/', 1)[-1]:
        stem, dot, ext = full_url.rpartition('.')
        return f"{stem}_crop_555x300.{ext}"
    return ''


def download_bundle_photos(photo_urls, bundle_id):
    """Download every photo for a bundle. Returns (filenames, variants).

    Saves as {bundle_id}_{idx}.{ext}, idx starting at 1. Idempotent: skips
    files already on disk. For each URL we try the full-res first, fall back
    to the _crop_555x300 thumbnail if the original 404s.
    """
    if not photo_urls:
        return ([], [])

    safe_id = clean_id(bundle_id)
    saved_filenames = []
    saved_variants = []

    for idx, full_url in enumerate(photo_urls, start=1):
        # Determine extension from URL
        ext = '.jpg'
        last = full_url.rsplit('/', 1)[-1] if '/' in full_url else full_url
        if '.' in last:
            ext = '.' + last.rsplit('.', 1)[1].lower()

        filename = f"{safe_id}_{idx}{ext}"
        filepath = IMAGES_DIR / filename

        # Idempotent: skip if already on disk
        if filepath.exists() and filepath.stat().st_size > 0:
            saved_filenames.append(filename)
            saved_variants.append('cached')
            continue

        # Try full first, then thumbnail fallback
        thumb_url = _thumb_url_from_full(full_url)
        candidates = [('full', full_url), ('thumb', thumb_url)]

        wrote = False
        for variant, url in candidates:
            if not url:
                continue
            if _download_one_image(url, filepath, bundle_id, variant):
                saved_filenames.append(filename)
                saved_variants.append(variant)
                wrote = True
                break
        if not wrote:
            # All variants failed; record_failure was already called by _download_one_image
            pass

    return (saved_filenames, saved_variants)


# -- CSV ---------------------------------------------------------------------

CSV_FIELDS = [
    # Core identity
    'bundle_id', 'material_name', 'composition', 'block', 'bundle_ref',
    # Material spec
    'finish', 'thickness', 'quality', 'classification', 'color',
    # Inventory state
    'slab_count', 'slabs', 'slab_count_actual', 'slabs_available',
    'display_status', 'location', 'country_code', 'country',
    # Dimensions/area
    'average_size', 'avg_size_metric', 'avg_size_imperial',
    'total_sqft', 'total_sqmt', 'total_area_summary',
    # Per-slab arrays (pipe-separated)
    'slab_ids', 'slab_numbers',
    'slab_widths_m', 'slab_heights_m',
    'slab_lengths_in', 'slab_heights_in',
    'slab_areas_sqmt', 'slab_areas_sqft',
    # Pricing (empty until authenticated)
    'currency',
    'price_main', 'price_main_old',
    'price_secondary', 'price_secondary_old',
    'price_total_bundle',
    'price_1', 'price_old_1', 'price_2', 'price_old_2',
    # Description, video, links
    'description', 'video_url', 'kitchen_visualizer_url',
    # Image references
    'image_filename_remote', 'image_url_full', 'image_url_thumb',
    'photo_urls', 'photo_count',
    'image_filenames_local', 'image_variants',
    # Meta
    'scrape_timestamp',
]


def open_csv():
    fh = open(OUTPUT_CSV, 'w', newline='', encoding='utf-8-sig', buffering=1)
    writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction='ignore')
    writer.writeheader()
    fh.flush()
    return fh, writer


def append_row(fh, writer, row):
    writer.writerow(row)
    fh.flush()


# -- Main --------------------------------------------------------------------

def main():
    IMAGES_DIR.mkdir(exist_ok=True)
    started = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    logger.info("=" * 70)
    logger.info("BRUMA GRANITE INVENTORY SCRAPER")
    logger.info("=" * 70)
    logger.info("Started:  %s", started)
    logger.info("CSV out:  %s", OUTPUT_CSV)
    logger.info("Images:   %s", IMAGES_DIR)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("")

    # Cloudflare warm-up
    warm_up_session()

    # First batch tells us total volume
    logger.info("\nFetching first batch (inicio=0)...")
    first_batch = fetch_page(0)
    if not first_batch:
        logger.error("First batch was empty. Aborting.")
        return

    logger.info("")
    logger.info("Page size:    %d bundles per listing call", PAGE_SIZE)
    logger.info("Page delay:   %.1f-%.1fs (long break of %.0f-%.0fs every %d pages)",
                PAGE_DELAY_MIN, PAGE_DELAY_MAX,
                LONG_BREAK_MIN, LONG_BREAK_MAX, LONG_BREAK_EVERY_N_PAGES)
    logger.info("Detail delay: %.1f-%.1fs per bundle detail call",
                DETAIL_DELAY_MIN, DETAIL_DELAY_MAX)
    logger.info("Image delay:  %.1f-%.1fs per image", IMAGE_DELAY_MIN, IMAGE_DELAY_MAX)
    logger.info("=" * 70)

    fh, writer = open_csv()
    written = 0
    interrupted = False

    def process_batch(batch):
        nonlocal written
        for item in batch:
            row = parse_bundle(item)
            row['scrape_timestamp'] = started
            written += 1

            bundle_id = row['bundle_id']
            logger.info("  [%d] %s - %s (block %s, %s)",
                        written, bundle_id, row['material_name'],
                        row['block'], row['thickness'])

            # Detail call to enrich with classification, color, multi-photo, slabs, etc.
            _sleep_jitter(DETAIL_DELAY_MIN, DETAIL_DELAY_MAX)
            detail = fetch_detail(bundle_id)
            row = merge_detail(row, detail)

            # Download every photo from fotos[]. Falls back to listing's fotoPrincipal
            # if the detail call failed and we have no fotos list.
            photo_urls_str = row.get('photo_urls') or ''
            if photo_urls_str:
                photo_urls = [u.strip() for u in photo_urls_str.split('|') if u.strip()]
            elif row.get('image_url_full'):
                photo_urls = [row['image_url_full']]
            else:
                photo_urls = []

            local_files, variants = download_bundle_photos(photo_urls, bundle_id)
            row['image_filenames_local'] = ' | '.join(local_files)
            row['image_variants'] = ' | '.join(variants)
            append_row(fh, writer, row)

    try:
        # First batch already in hand
        logger.info("\nBatch inicio=0")
        process_batch(first_batch)

        inicio = PAGE_SIZE
        page_idx = 1
        while True:
            logger.info("\nBatch inicio=%d", inicio)
            try:
                if LONG_BREAK_EVERY_N_PAGES and page_idx % LONG_BREAK_EVERY_N_PAGES == 0:
                    _sleep_jitter(LONG_BREAK_MIN, LONG_BREAK_MAX, label='long break')
                else:
                    _sleep_jitter(PAGE_DELAY_MIN, PAGE_DELAY_MAX)

                batch = fetch_page(inicio)
                if not batch:
                    logger.info("Empty batch at inicio=%d. Done paginating.", inicio)
                    break
                process_batch(batch)

                inicio += PAGE_SIZE
                page_idx += 1
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error("Batch inicio=%d failed after retries: %s. Continuing.",
                             inicio, e)
                record_failure('page', page_inicio=inicio, attempts=MAX_RETRIES, error=str(e))
                inicio += PAGE_SIZE
                page_idx += 1
    except KeyboardInterrupt:
        interrupted = True
        logger.warning("\nInterrupted by user. Saving partial results and exiting...")
    finally:
        fh.close()
        write_failures_csv()

    finished = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    imgs_on_disk = len(list(IMAGES_DIR.glob('*'))) if IMAGES_DIR.exists() else 0
    page_failures = sum(1 for f in _failures if f['kind'] == 'page')
    detail_failures = sum(1 for f in _failures if f['kind'] == 'detail')
    image_failures = sum(1 for f in _failures if f['kind'] == 'image')

    logger.info("")
    logger.info("=" * 70)
    logger.info("DONE" + (" (interrupted)" if interrupted else ""))
    logger.info("=" * 70)
    logger.info("Started:         %s", started)
    logger.info("Finished:        %s", finished)
    logger.info("Bundles:         %d written to CSV", written)
    logger.info("Images:          %d files on disk in %s", imgs_on_disk, IMAGES_DIR)
    logger.info("Page failures:   %d", page_failures)
    logger.info("Detail failures: %d", detail_failures)
    logger.info("Image failures:  %d", image_failures)
    logger.info("CSV:             %s", OUTPUT_CSV)
    logger.info("Log file:        %s", LOG_FILE)
    if _failures:
        logger.info("Failures CSV:    %s", FAILURES_CSV)


if __name__ == '__main__':
    main()