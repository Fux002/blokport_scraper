"""
Polonine (SlabWare) Full Inventory Scraper
==========================================
Sibling of the MarenoStone scraper, but a completely different stack.

MarenoStone was WordPress + WooCommerce, so we could hit the WC Store API and
page through clean JSON. Polonine runs on **SlabWare**, a cloud ASP.NET WebForms
slab-inventory platform (polonine.slabware.com/FullInventory.aspx). There is NO
public JSON API: the inventory grid is injected client-side after page load via
AJAX / UpdatePanel postbacks driven by __VIEWSTATE, with infinite-scroll lazy
loading and a couple of modal overlays (cookie banner + welcome popup). A plain
`requests` scrape of the .aspx URL returns only the empty filter shell.

So this scraper drives a real headless browser (Playwright/Chromium):

  1. Opens FullInventory.aspx with the tenant share token (the long ?S=... blob).
  2. Dismisses the cookie banner / welcome / out-of-service modals.
  3. Switches to the densest data view (photo or list) and waits past the spinner.
  4. Scrolls to the bottom repeatedly until the card count stops growing
     (handles SlabWare's lazy "load more on scroll" grid).
  5. Extracts every slab/bundle card. Because SlabWare's per-tenant markup class
     names are not 100% stable, extraction uses three layers, best-first:
        (a) NETWORK SNIFF  - capture the grid's own XHR/postback payloads
                             (JSON or HTML fragments) straight off the wire.
        (b) KNOWN SELECTORS - a tunable list of SlabWare card selectors.
        (c) GENERIC FALLBACK - find the largest cluster of repeated sibling
                             nodes that each contain an <img>, then regex the
                             text for thickness / dimensions / price / lot.
     On the first run it also dumps the rendered DOM + a screenshot + every
     captured response to ./debug_polonine/ so you can confirm/tune selectors
     against the live tenant in one pass.
  6. Downloads all slab photos (retry/backoff, polite jitter, resumable, with
     the browser session cookies copied into the requests session so any
     session-gated image URLs still resolve).

Prices on SlabWare public sites are gated to logged-in customers, so the price
column is usually blank - we capture it anyway in case the tenant exposes it.

------------------------------------------------------------------------------
SETUP (run once on your machine):
    pip install playwright requests
    playwright install chromium
RUN:
    python scraper_polonine.py
    # or point it at a different tenant / paste a fresh token:
    python scraper_polonine.py --url "https://polonine.slabware.com/FullInventory.aspx?S=...."
    python scraper_polonine.py --headful          # watch it work
    python scraper_polonine.py --no-details        # skip per-card detail open
    python scraper_polonine.py --no-images         # CSV only, skip downloads
------------------------------------------------------------------------------
"""

import argparse
import csv
import json
import logging
import random
import re
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urljoin

import requests

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:  # pragma: no cover - guidance only
    raise SystemExit(
        "Playwright is required. Install with:\n"
        "    pip install playwright requests\n"
        "    playwright install chromium"
    )

# ============================================================================
# TUNABLE KNOBS
# ============================================================================

# --- Target -----------------------------------------------------------------
# The full FullInventory URL including the tenant share token (?S=...).
# Paste a fresh token here, or override at runtime with --url.
DEFAULT_URL = (
    "https://polonine.slabware.com/FullInventory.aspx?S="
    "9t5AptkpH/x2GD3oStI0wQzByO78puNBq5ZOBU2x8a1DsWdZJ9W+UFgwpAvr7qTXgoGcUrLnmVFKc1f53xV19kVT86meSR82TYzwo9Agi4tIXRQahJ4warnLVR8jPRR5Nlzhv/4jv1JmwSC9pdc1APdC6JsDPn80c4HIItTOWsDTMGQ/m4KWvF/vuGG1xQjPN2naAtXme+bu5+uO0hBOcWWESF+wB2pQt8LvkO+h5JyX1H2r3rTAczO9+1aOnEINoQybgacnN/c/T1fFwsW+EWUS8XgpOiJkM6UPR5tsTEeNWPUQ1RNMPKHbkoFCDNu0cyNLSh2evfTLqOzz2DeOB1jvLXtdEm5CGTjLQbSazi5YDZ3J/iI+wjOvyyKDLqsjQpxThT9Aqnouk+4m5j0bMQ0xdINjDRTu5eY3xwn8REhAbLryAPUB0NGE9hlAATDyNv72E4Y62lg+nOBU9naNm8Ga5dwl2DeNXGmFgFhCQjXfbWq8IbhX8Yw0H6IVORAP"
)

# Which inventory view to scrape. "photo" = one card per slab/photo (richest for
# images); "list" = bundles list; "" = default grid.
VIEW = "photo"

# --- File paths -------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent if "__file__" in globals() else Path.cwd()
IMAGES_DIR = SCRIPT_DIR / "images_polonine"
DEBUG_DIR = SCRIPT_DIR / "debug_polonine"
TIMESTAMP_TAG = datetime.now().strftime("%Y%m%d_%H%M%S")
OUTPUT_CSV = SCRIPT_DIR / f"polonine_products_{TIMESTAMP_TAG}.csv"
LOG_FILE = SCRIPT_DIR / f"polonine_scraper_{TIMESTAMP_TAG}.log"
FAILURES_CSV = SCRIPT_DIR / f"polonine_failures_{TIMESTAMP_TAG}.csv"

# --- Browser ----------------------------------------------------------------
HEADLESS = True
NAV_TIMEOUT_MS = 60_000          # page.goto timeout
GRID_WAIT_MS = 25_000            # how long to wait for first cards to appear
PAGE_SETTLE_MS = 2_500           # pause after load before interacting

# --- Lazy-load scrolling ----------------------------------------------------
SCROLL_PAUSE_MIN = 1.2           # seconds between scroll steps
SCROLL_PAUSE_MAX = 2.4
SCROLL_MAX_ROUNDS = 400          # hard cap on scroll iterations
SCROLL_STABLE_ROUNDS = 4         # stop after N rounds with no new cards
PRIME_SCROLL_ROUNDS = 3          # quick scrolls to trigger a couple of real
                                 # ObterListaBundles requests for pagination
                                 # detection before we replay the whole endpoint

# --- Per-card detail open ---------------------------------------------------
# OFF by default: the cards already carry image URLs, and clicking 200+ cards is
# slow and fragile. Enable with --details only if you need extra full-res images
# that aren't present on the card itself.
OPEN_DETAILS = False
DETAIL_DELAY_MIN = 0.5
DETAIL_DELAY_MAX = 1.3

# --- Image download ---------------------------------------------------------
DOWNLOAD_IMAGES = True
IMAGE_DELAY_MIN = 0.4
IMAGE_DELAY_MAX = 1.0
LONG_BREAK_EVERY_N_PRODUCTS = 25
LONG_BREAK_MIN = 10.0
LONG_BREAK_MAX = 10.0

# --- Retry / timeouts -------------------------------------------------------
MAX_RETRIES = 5
BACKOFF_BASE = 2.0
RATE_LIMIT_BACKOFF = 10.0 # 60
IMAGE_TIMEOUT = 10
API_TIMEOUT = 10                 # per ObterListaBundles replay request (seconds)
API_PAGE_DELAY_MIN = 0.6         # polite pause between replayed pages (avoid 429)
API_PAGE_DELAY_MAX = 1.4
API_MAX_PAGES = 500              # hard safety cap

# --- Per-product detail enrichment ------------------------------------------
# Exact endpoints / shapes, reverse-engineered from cardsBundles.js,
# detalheBundle.js and a real ObterListaBundles response:
#   LIST:   POST /FullInventory.aspx/ObterListaBundles  body {inicio, json}
#           -> r.d.Bundles  (a DOUBLE-ENCODED JSON string of 40 records)
#   DETAIL: POST /FullInventory.aspx/DetalheBundle       body {IdBundle, IdCampanha}
#           -> r.d.Bundle   (full object incl. chapas[] = per-slab rows)
#   IMAGES: /backendGranite/cadastros/bundles/fotos/{id}/{fotoPrincipal}
LIST_ENDPOINT_PATH = "/FullInventory.aspx/ObterListaBundles"
DETAIL_ENDPOINT_PATH = "/FullInventory.aspx/DetalheBundle"
LIST_OFFSET_FIELD = "inicio"          # body field = count loaded so far
IMG_BASE_PATH = "/backendGranite/cadastros/bundles/fotos"  # /{id}/{filename}

ENRICH_DETAILS = True
DETAIL_PATH = "/Product-Details.aspx"   # human-facing detail URL (for detail_url col)
DETAIL_ID_PARAM = "ID"
DETAIL_DISCOVER_WAIT_MS = 15_000
DETAIL_PAGE_DELAY_MIN = 0.6      # polite pause between detail requests
DETAIL_PAGE_DELAY_MAX = 1.4
DETAIL_LONG_BREAK_EVERY = 20     # extra cooldown every N detail fetches
DETAIL_LONG_BREAK_MIN = 8.0
DETAIL_LONG_BREAK_MAX = 12.0

# --- User agent rotation (for image session; browser sets its own too) ------
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

# --- Card selectors, tried in order (layer b) -------------------------------
# SlabWare tenants vary; first match wins. The generic fallback (layer c) runs
# if none of these yield a sensible number of cards. After the first run, look
# at debug_polonine/rendered_dom.html and add the exact selector here.
# NOTE: counting/extraction now filter to VISIBLE cards and exclude template
# nodes (see EXCLUDE_CLASS_SUBSTRINGS), so broad selectors like [class*='bundle']
# no longer pick up the hidden <div class="detalhe-bundle"> detail template.
CARD_SELECTORS = [
    "div.bundle",
    "div.item-bundle",
    "li.bundle",
    "div.produto",
    "div.product",
    "a.materialItem",
    "div.material-card",
    "div.card-material",
    "[class*='bundle']",
    "[class*='produto']",
    "[class*='material'][class*='card']",
]

# Any element whose class contains one of these substrings is NOT a real card
# (they are hidden detail templates / modals). Matched case-insensitively.
EXCLUDE_CLASS_SUBSTRINGS = ["detalhe", "detail", "template", "modelo", "modal", "popup"]

# Promo/section widget labels to drop if DOM fallback ever runs (these are the
# carousel strips like the "NEW ARRIVALS" row, not real inventory bundles).
_SECTION_LABELS = {
    "newarrivals", "recommended", "onsale", "comingsoon", "remnant", "remnants",
    "all", "filterby", "fullinventory", "listofbundles", "listofphotos",
}

# Selectors whose text we try to mine inside each card (best-effort).
FIELD_HINTS = {
    "material": ["[class*='material']", "[class*='nome']", "h3", "h4", ".title"],
    "thickness": ["[class*='thick']", "[class*='espess']"],
    "lot": ["[class*='lot']", "[class*='bundle']", "[class*='lote']"],
    "price": ["[class*='price']", "[class*='preco']", "[class*='valor']"],
    "location": ["[class*='local']", "[class*='location']"],
}

# ============================================================================

# -- Logging -----------------------------------------------------------------
logger = logging.getLogger("polonine")
logger.setLevel(logging.DEBUG)
logger.propagate = False

_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_console)

_file = logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8")
_file.setLevel(logging.DEBUG)
_file.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logger.addHandler(_file)


_failures = []


def record_failure(kind, **details):
    entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        **details,
    }
    _failures.append(entry)
    logger.warning("FAILURE [%s] %s", kind,
                   " ".join(f"{k}={v!r}" for k, v in details.items()))


def write_failures_csv():
    if not _failures:
        return
    keys = ["timestamp", "kind", "product_id", "url", "status", "attempts", "error"]
    with open(FAILURES_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for entry in _failures:
            writer.writerow(entry)


def _sleep_jitter(low, high, label=None):
    secs = random.uniform(low, high)
    if label:
        logger.info("    Waiting %.1fs (%s)...", secs, label)
    time.sleep(secs)


def clean_text(s):
    return unescape((s or "").strip())


def clean_id(s):
    return re.sub(r'[<>:"/\\|?*\s]', "", str(s or ""))


# ============================================================================
# NETWORK SNIFFING (layer a)
# ============================================================================
# SlabWare loads the grid via XHR/postback. We attach a response listener and
# stash anything that looks like inventory data (JSON, or HTML fragments from an
# UpdatePanel). On the first run these land in debug_polonine/ so you can see
# exactly where the real data comes from and, if it's clean JSON, parse it.

_captured_responses = []
_bundle_requests = []      # POSTs to ObterListaBundles (list endpoint)
_pagemethod_requests = []  # all POSTs to *.aspx/<Method> (for detail discovery)

_PAGEMETHOD_RE = re.compile(r"\.aspx/[A-Za-z_]\w*(?:\?|$)", re.I)


def _attach_sniffer(page):
    def on_request(request):
        try:
            if request.method != "POST":
                return
            url = request.url
            if "slabware.com" not in urlparse(url).netloc:
                return
            if not _PAGEMETHOD_RE.search(url):
                return
            entry = {
                "url": url,
                "headers": dict(request.headers or {}),
                "post_data": request.post_data,
            }
            _pagemethod_requests.append(entry)
            if BUNDLE_ENDPOINT_HINT in url:
                _bundle_requests.append(entry)
        except Exception:
            pass

    def on_response(response):
        try:
            url = response.url
            ctype = (response.headers or {}).get("content-type", "")
            # Only bother with same-origin data-ish responses
            if "slabware.com" not in urlparse(url).netloc:
                return
            interesting = (
                "json" in ctype
                or "FullInventory" in url
                or "Material" in url
                or "Bundle" in url
                or "Inventory" in url
                or url.endswith(".asmx")
                or url.endswith(".ashx")
            )
            if not interesting:
                return
            _captured_responses.append({
                "url": url,
                "status": response.status,
                "content_type": ctype,
                "response": response,
            })
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)


def _dump_captured():
    if not _captured_responses:
        logger.info("Network sniff: no candidate data responses captured.")
        return []
    DEBUG_DIR.mkdir(exist_ok=True)
    logger.info("Network sniff: %d candidate responses captured -> %s",
                len(_captured_responses), DEBUG_DIR)
    index = []
    for i, item in enumerate(_captured_responses):
        rec = {k: item[k] for k in ("url", "status", "content_type")}
        body = None
        try:
            body = item["response"].text()
            item["body"] = body  # cache for the API collector
            ext = "json" if "json" in item["content_type"] else "txt"
            (DEBUG_DIR / f"response_{i:03d}.{ext}").write_text(
                body, encoding="utf-8", errors="ignore")
            rec["saved"] = f"response_{i:03d}.{ext}"
            rec["bytes"] = len(body)
        except Exception as e:
            rec["error"] = str(e)
        # Flag responses that parse as a JSON array of records (a likely data
        # endpoint we could target directly instead of DOM-scraping).
        if body:
            rec.update(_inspect_json_body(body, rec.get("saved")))
        index.append(rec)
    (DEBUG_DIR / "captured_index.json").write_text(
        json.dumps(index, indent=2), encoding="utf-8")
    return index


def _inspect_json_body(body, saved_name):
    """If body is JSON holding a list of record-like dicts, summarise it so we
    know whether a clean data endpoint exists (and what keys it exposes)."""
    info = {}
    try:
        data = json.loads(body)
    except Exception:
        return info
    # ASP.NET often wraps payloads as {"d": [...]} or {"d": "...json..."}.
    candidates = [data]
    if isinstance(data, dict):
        candidates.extend(data.values())
        if isinstance(data.get("d"), str):
            try:
                candidates.append(json.loads(data["d"]))
            except Exception:
                pass
    for c in candidates:
        if isinstance(c, list) and c and isinstance(c[0], dict):
            info["json_records"] = len(c)
            info["json_keys"] = sorted(c[0].keys())[:40]
            logger.info("  >> %s looks like a DATA ENDPOINT: %d records, keys: %s",
                        saved_name or "(response)", len(c),
                        ", ".join(info["json_keys"][:12]) + ("..." if len(info["json_keys"]) > 12 else ""))
            break
    return info


# ============================================================================
# API BUNDLE PARSING (primary data path)
# ============================================================================
# polonine.slabware.com renders its grid by POSTing to the ASP.NET PageMethod
#   FullInventory.aspx/ObterListaBundles
# which returns JSON (~40 bundles per page, wrapped ASP.NET-style as {"d": ...}).
# As the browser scrolls, every page is fetched; we collect those responses and
# parse them directly instead of scraping the rendered cards.

# Endpoint URL fragment that identifies the bundle-list responses.
BUNDLE_ENDPOINT_HINT = "ObterListaBundles"

# Best-effort key aliases (EN + PT). First record key whose normalised name
# CONTAINS one of these wins. Raw JSON is always preserved in `raw_json`, so a
# missed mapping is never data loss - just add the real key here after the run.
BUNDLE_FIELD_ALIASES = {
    "material":         ["materialname", "materialnome", "material", "nomematerial", "nome", "produto", "name"],
    "stone_type":       ["composic", "stonetype", "tipopedra", "naturalstone", "stone"],
    "color":            ["cor", "color", "colour"],
    "finish":           ["acabamento", "finish", "surface", "superficie"],
    "quality":          ["qualidade", "quality", "grade", "nivel"],
    "classification":   ["classificac", "classification", "classif"],
    "type":             ["tipomaterial", "categoria", "tipo", "type"],
    "origin":           ["origem", "origin", "pais", "country"],
    "location":         ["localizacao", "armazem", "deposito", "warehouse", "location", "local"],
    "thickness":        ["espessura", "thickness", "thick"],
    "lot_bundle":       ["numerolote", "lote", "bundlenumber", "bundle", "bloco", "block", "lot"],
    "dimension_height": ["altura", "height"],
    "dimension_length": ["comprimento", "length"],
    "width":            ["largura", "width", "largo"],
    "area":             ["areatotal", "metragem", "area", "sqft", "squarefeet", "totalarea"],
    "price":            ["precom2", "preco", "price", "valor", "amount"],
    "slab_count":       ["quantidadechapas", "numerochapas", "chapas", "slabs", "quantidade", "qtd", "pieces", "pecas"],
}

# Keys/strings that look like image references.
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")
_IMG_PATH_HINTS = ("/images/", "/fotos/", "/photos/", "/uploads/", "foto", "photo", "image", "thumb")


def _norm(k):
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _scalar(v):
    if v is None or isinstance(v, (dict, list)):
        return ""
    return str(v).strip()


def _find_key(rec, aliases, exclude=()):
    """Return the first record key matching an alias, honouring alias PRIORITY
    (aliases earlier in the list win) rather than record key order."""
    for a in aliases:
        for k in rec:
            if k in exclude:
                continue
            if a in _norm(k):
                return k
    return None


def _match_alias(rec, aliases, exclude=()):
    k = _find_key(rec, aliases, exclude)
    return _scalar(rec[k]) if k is not None else ""


def _bundle_id_key(rec):
    for k in rec:
        nk = _norm(k)
        if "bundle" in nk and "id" in nk:
            return k
    for k in rec:
        if _norm(k) in ("codigo", "code", "cod"):
            return k
    for k in rec:
        nk = _norm(k)
        if nk == "id" or nk.endswith("id"):
            return k
    return None


def _bundle_id(rec):
    k = _bundle_id_key(rec)
    return _scalar(rec[k]) if k is not None else ""


def _abs_img(s, origin):
    s = s.strip().replace("\\", "/")
    if s.startswith("http://") or s.startswith("https://"):
        return s
    if s.startswith("//"):
        return "https:" + s
    if s.startswith("/"):
        return origin + s
    # bare filename -> SlabWare serves these from /images/
    return f"{origin}/images/{s}"


def _looks_like_image(s):
    low = s.lower().split("?")[0]
    if low.endswith(_IMG_EXT):
        return True
    return any(h in low for h in ("/images/", "/fotos/", "/photos/", "/uploads/"))


def _collect_bundle_images(obj, origin, out, key_hint=""):
    """Recursively gather image URLs from a bundle record."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_bundle_images(v, origin, out, _norm(k))
    elif isinstance(obj, list):
        for v in obj:
            _collect_bundle_images(v, origin, out, key_hint)
    elif isinstance(obj, str):
        s = obj.strip()
        if not s:
            return
        photo_key = any(h in key_hint for h in ("foto", "photo", "image", "img", "thumb"))
        if _looks_like_image(s) or (photo_key and ("." in s or "/" in s)):
            out.append(_abs_img(s, origin))


def _unwrap_records(payload):
    """Return a list of bundle dicts from an ObterListaBundles payload.

    Real shape: {"d": {"Permissoes": {...}, "Bundles": "<json-string of [ {...} ]>"}}
    The Bundles value is a DOUBLE-ENCODED JSON string, so we json.loads it again.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        # Preferred: d.Bundles (string -> list)
        d = payload.get("d", payload)
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except Exception:
                d = payload.get("d")
        if isinstance(d, dict) and "Bundles" in d:
            b = d["Bundles"]
            if isinstance(b, str):
                try:
                    b = json.loads(b)
                except Exception:
                    b = None
            if isinstance(b, list):
                return [r for r in b if isinstance(r, dict)]
        # Generic fallbacks: any value that is (or decodes to) a list of dicts.
        containers = [payload]
        if isinstance(d, dict):
            containers.append(d)
        for cont in containers:
            for v in cont.values():
                if isinstance(v, str) and v[:1] in ("[", "{"):
                    try:
                        v = json.loads(v)
                    except Exception:
                        continue
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    return v
    return []


def collect_api_bundles(index):
    """Parse all captured ObterListaBundles JSON bodies -> deduped bundle list."""
    bundles = []
    seen = set()
    n_429 = 0
    for item in _captured_responses:
        url = item.get("url", "")
        if BUNDLE_ENDPOINT_HINT not in url:
            continue
        if item.get("response") is not None and getattr(item["response"], "status", 200) == 429:
            n_429 += 1
        body = item.get("body")
        if not body:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        for rec in _unwrap_records(data):
            bid = _bundle_id(rec)
            key = bid or json.dumps(rec, sort_keys=True)[:300]
            if key in seen:
                continue
            seen.add(key)
            bundles.append(rec)
    if n_429:
        logger.warning("ObterListaBundles returned %d rate-limit (429) responses - "
                       "some pages may be missing. Increase SCROLL_PAUSE_* and rerun "
                       "if the bundle count looks short.", n_429)
    return bundles


# -- Reliable replay of the bundle endpoint ----------------------------------
# Passive response bodies get evicted by the browser, so the dependable way to
# get every page is to REPLAY the observed POST through the browser's own
# request context (same cookies/session) and read each response immediately.

_PAGE_FIELD_HINTS = ["pageindex", "indice", "pagina", "pagenumber", "page",
                     "registroinicial", "indiceinicial", "start", "skip", "offset"]
_PAGE_SIZE_HINTS = ["itensporpagina", "registrosporpagina", "quantidaderegistros",
                    "pagesize", "quantidade", "take", "length", "limit", "qtd"]


def _parsed_request_bodies():
    out = []
    for r in _bundle_requests:
        pd = r.get("post_data")
        if not pd:
            continue
        try:
            obj = json.loads(pd)
            if isinstance(obj, dict):
                out.append(obj)
        except Exception:
            pass
    return out


def _detect_pagination(bodies):
    """Return (page_field, start, step). Prefer diffing real captured requests;
    fall back to a name heuristic if we only saw one request."""
    # Diff approach: find an int field whose value varies across requests.
    if len(bodies) >= 2:
        keys = set().union(*(b.keys() for b in bodies))
        best = None
        for k in keys:
            vals = [b[k] for b in bodies if isinstance(b.get(k), int)]
            uv = sorted(set(vals))
            if len(uv) >= 2:
                step = min((b - a) for a, b in zip(uv, uv[1:]) if b > a)
                # Prefer a field whose name looks paginate-y; else take any.
                score = 1 if any(h in _norm(k) for h in _PAGE_FIELD_HINTS) else 0
                cand = (score, k, uv[0], step)
                if best is None or cand[0] > best[0]:
                    best = cand
        if best:
            return best[1], best[2], best[3]
    # Name heuristic on a single body.
    if bodies:
        b = bodies[0]
        for h in _PAGE_FIELD_HINTS:
            for k in b:
                if h in _norm(k) and isinstance(b[k], int):
                    return k, b[k], 1
    return None, None, None


def _detect_page_size(body):
    for h in _PAGE_SIZE_HINTS:
        for k in body:
            if h in _norm(k) and isinstance(body[k], int) and body[k] > 1:
                return body[k]
    return None


def _page_post_json(page, url, body):
    """POST JSON from INSIDE the page context via fetch(). This carries the real
    browser cookies, Origin, Referer and User-Agent, so it passes the WAF/anti-bot
    checks that block a bare context.request.post (which returns 403). Returns
    (status, text)."""
    js = """async ([url, bodyStr]) => {
        try {
            const r = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Accept': 'application/json, text/javascript, */*; q=0.01'
                },
                body: bodyStr,
                credentials: 'include'
            });
            const t = await r.text();
            return { status: r.status, text: t };
        } catch (e) {
            return { status: -1, text: String(e) };
        }
    }"""
    try:
        res = page.evaluate(js, [url, json.dumps(body)])
        return res.get("status"), res.get("text")
    except Exception as e:
        return None, str(e)


def fetch_all_bundles_via_replay(page):
    """Replay the captured ObterListaBundles POST page-by-page (in-page fetch);
    return records."""
    if not _bundle_requests:
        logger.info("Replay: no ObterListaBundles request was observed to replay.")
        return []
    tmpl = _bundle_requests[-1]
    try:
        body = json.loads(tmpl["post_data"]) if tmpl.get("post_data") else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    bodies = _parsed_request_bodies()
    field, start, step = _detect_pagination(bodies)
    # SlabWare pages by OFFSET: body['inicio'] = number of bundles loaded so far.
    if field is None and any(_norm(LIST_OFFSET_FIELD) in _norm(k) for k in body):
        field, start, step = LIST_OFFSET_FIELD, 0, 40
    offset_mode = bool(field) and (
        any(h in _norm(field) for h in ("inicio", "start", "skip", "offset", "registroinicial"))
        or (step and step > 1))
    page_size = _detect_page_size(body)
    logger.info("Replay: field=%r start=%r step=%r offset_mode=%s page_size=%r",
                field, start, step, offset_mode, page_size)

    url = tmpl["url"]
    all_recs, seen = [], set()
    page_num = start if start is not None else 0
    pages = 0
    while pages < API_MAX_PAGES:
        if field is not None:
            body[field] = len(all_recs) if offset_mode else page_num
        status, text = _page_post_json(page, url, body)
        if status == 429:
            logger.warning("Replay 429 at offset %d; sleeping %.0fs...",
                           len(all_recs), RATE_LIMIT_BACKOFF)
            time.sleep(RATE_LIMIT_BACKOFF)
            continue
        if status != 200:
            record_failure("api_post", url=url, status=status)
            break
        try:
            recs = _unwrap_records(json.loads(text))
        except Exception as e:
            record_failure("api_parse", url=url, error=str(e))
            break

        new = 0
        for r in recs:
            bid = _bundle_id(r) or json.dumps(r, sort_keys=True)[:300]
            if bid in seen:
                continue
            seen.add(bid)
            all_recs.append(r)
            new += 1
        pages += 1
        logger.info("  replay page %d (offset %d): %d records (%d new, %d total)",
                    pages, len(all_recs) - new, len(recs), new, len(all_recs))

        if field is None:
            break                       # single-shot endpoint, nothing to page
        if not recs or new == 0:
            break                       # ran past the end / endpoint clamped
        if page_size and len(recs) < page_size:
            break                       # short final page
        page_num += 1
        _sleep_jitter(API_PAGE_DELAY_MIN, API_PAGE_DELAY_MAX)

    logger.info("Replay collected %d unique bundles across %d page(s).",
                len(all_recs), pages)
    return all_recs


def _merge_bundles(*lists):
    """Union bundle lists, deduped by bundle id, preserving first-seen order."""
    out, seen = [], set()
    for lst in lists:
        for rec in lst:
            bid = _bundle_id(rec) or json.dumps(rec, sort_keys=True)[:300]
            if bid in seen:
                continue
            seen.add(bid)
            out.append(rec)
    return out


# -- Per-product detail enrichment -------------------------------------------
# Product-Details.aspx?ID=<id> loads its data via its own PageMethod. We visit
# one product page to discover that endpoint (URL + body shape + which field
# carries the id), then replay it per bundle to pull full attributes + photos.

def _clean_post_headers(raw):
    skip = {"content-length", "host", "cookie", "connection", "accept-encoding",
            ":authority", ":method", ":path", ":scheme"}
    headers = {k: v for k, v in (raw or {}).items() if k.lower() not in skip}
    headers.setdefault("Content-Type", "application/json; charset=UTF-8")
    headers.setdefault("X-Requested-With", "XMLHttpRequest")
    return headers


def _detail_record_from_response(text):
    """DetalheBundle returns {"d": {"Bundle": {...}, "Permissoes": {...}, ...}}.
    Return the Bundle object (which includes chapas[] = per-slab rows)."""
    try:
        data = json.loads(text)
    except Exception:
        return None
    d = data.get("d", data) if isinstance(data, dict) else data
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            pass
    if isinstance(d, dict):
        if isinstance(d.get("Bundle"), dict):
            return d["Bundle"]
        return d
    if isinstance(d, list) and d and isinstance(d[0], dict):
        return d[0]
    return None


def build_known_detail_endpoint(base_origin):
    """The detail PageMethod is known exactly from detalheBundle.js."""
    headers = None
    for r in _pagemethod_requests:
        headers = _clean_post_headers(r.get("headers"))
        break
    if headers is None:
        headers = {"Content-Type": "application/json; charset=UTF-8",
                   "X-Requested-With": "XMLHttpRequest"}
    return {
        "url": f"{base_origin}{DETAIL_ENDPOINT_PATH}",
        "body": {"IdBundle": None, "IdCampanha": None},
        "id_field": "IdBundle",
        "id_is_int": True,
        "headers": headers,
    }


def fetch_detail_record(page, endpoint, bundle_id):
    """Fetch DetalheBundle for one bundle id via in-page fetch; return the dict."""
    body = dict(endpoint["body"])
    val = bundle_id
    if endpoint.get("id_is_int"):
        try:
            val = int(bundle_id)
        except Exception:
            val = bundle_id
    body[endpoint["id_field"]] = val
    for attempt in range(2):
        status, text = _page_post_json(page, endpoint["url"], body)
        if status == 429:
            logger.warning("Detail 429 for id=%s; sleeping %.0fs...", bundle_id, RATE_LIMIT_BACKOFF)
            time.sleep(RATE_LIMIT_BACKOFF)
            continue
        if status != 200:
            record_failure("detail_post", product_id=bundle_id, status=status)
            return None
        return _detail_record_from_response(text)
    return None


def enrich_with_details(page, bundles, base_origin):
    """For every bundle, fetch its DetalheBundle record (in-page) and merge in."""
    if not bundles:
        return bundles
    sample_id = _bundle_id(bundles[0])
    if not sample_id:
        logger.warning("No bundle id on first record; skipping detail enrichment.")
        return bundles

    endpoint = build_known_detail_endpoint(base_origin)
    test = fetch_detail_record(page, endpoint, sample_id)
    if not test:
        logger.warning("Detail endpoint did not validate (id=%s). List data still "
                       "written in full; rerun later to enrich.", sample_id)
        return bundles

    try:
        (DEBUG_DIR / "first_detail.json").write_text(
            json.dumps(test, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Detail endpoint OK. Sample keys: %s",
                    ", ".join(list(test.keys())[:30]))
    except Exception:
        pass

    logger.info("Enriching %d bundles with detail data...", len(bundles))
    enriched = 0
    for i, b in enumerate(bundles):
        bid = _bundle_id(b)
        if not bid:
            continue
        det = fetch_detail_record(page, endpoint, bid)
        if det:
            bundles[i] = {**b, **det}  # detail fields win / add
            enriched += 1
        if (i + 1) % 25 == 0:
            logger.info("  detail %d/%d (%d enriched)", i + 1, len(bundles), enriched)
        _sleep_jitter(DETAIL_PAGE_DELAY_MIN, DETAIL_PAGE_DELAY_MAX)
        if DETAIL_LONG_BREAK_EVERY and (i + 1) % DETAIL_LONG_BREAK_EVERY == 0 and i + 1 < len(bundles):
            _sleep_jitter(DETAIL_LONG_BREAK_MIN, DETAIL_LONG_BREAK_MAX, label="detail cooldown")
    logger.info("Detail enrichment complete: %d/%d bundles enriched.", enriched, len(bundles))
    return bundles


def _flatten_leaves(obj, out=None):
    """Collect scalar leaves keyed by their immediate key name, first non-empty
    wins. Lets alias matching see fields nested inside detail-JSON sub-objects."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _flatten_leaves(v, out)
            elif v is not None and str(v).strip() != "":
                if k not in out:
                    out[k] = v
    elif isinstance(obj, list):
        for v in obj:
            _flatten_leaves(v, out)
    return out


def _strip_html(s):
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()


def _slab_img(origin, bundle_id, foto):
    """Build a full photo URL. SlabWare gives bare filenames in the LIST records
    but already-rooted paths (/backendGranite/...) in the DETAIL records, so we
    must not double-prefix."""
    foto = (foto or "").strip().replace("\\", "/")
    if not foto:
        return ""
    if foto.startswith("http://") or foto.startswith("https://"):
        return foto
    if foto.startswith("//"):
        return "https:" + foto
    if foto.startswith("/"):                       # already rooted path
        return origin + foto
    if "/" in foto:                                # relative path with folders
        return f"{origin}/{foto}"
    return f"{origin}{IMG_BASE_PATH}/{bundle_id}/{foto}"   # bare filename


def _g(rec, *keys):
    """First non-empty value among keys (case/space-insensitive key match)."""
    norm_map = {_norm(k): k for k in rec}
    for key in keys:
        actual = norm_map.get(_norm(key))
        if actual is not None:
            v = rec[actual]
            if v is not None and str(v).strip() != "":
                return v
    return ""


def parse_bundle(rec, idx, origin):
    """Map a SlabWare bundle record (ObterListaBundles row, optionally merged
    with the DetalheBundle object) to a flat CSV row using its exact field names.
    """
    bundle_id = _scalar(_g(rec, "id", "IdBundle")) or f"row{idx:05d}"

    # Photos: main + every slab photo (chapas[].Foto) + any fotos[] array.
    images = []
    foto_principal = _scalar(_g(rec, "fotoPrincipal", "FotoPrincipal"))
    if foto_principal:
        images.append(_slab_img(origin, bundle_id, foto_principal))
    chapas = _g(rec, "chapas", "Chapas") or []
    if isinstance(chapas, list):
        for ch in chapas:
            if isinstance(ch, dict):
                f = _scalar(_g(ch, "Foto"))
                if f:
                    images.append(_slab_img(origin, bundle_id, f))
    fotos = _g(rec, "fotos", "Fotos") or []
    if isinstance(fotos, list):
        for f in fotos:
            name = f if isinstance(f, str) else (f.get("Foto") or f.get("Url") if isinstance(f, dict) else "")
            if name:
                images.append(name if str(name).startswith("http") else _slab_img(origin, bundle_id, name))
    images = list(dict.fromkeys(u for u in images if u))

    # Slab measurements (from chapas[]); keep per-slab detail + a summary.
    slab_rows = []
    if isinstance(chapas, list):
        for ch in chapas:
            if not isinstance(ch, dict):
                continue
            slab_rows.append({
                "n":  _scalar(_g(ch, "Numero")),
                "h_cm": _scalar(_g(ch, "Altura")),
                "w_cm": _scalar(_g(ch, "Largura")),
                "h_in": _scalar(_g(ch, "Height")),
                "w_in": _scalar(_g(ch, "Length")),
                "sqft": _scalar(_g(ch, "TotalSqft")),
                "sqmt": _scalar(_g(ch, "TotalSqmt")),
            })
    first_slab = slab_rows[0] if slab_rows else {}

    slab_count = _scalar(_g(rec, "qtdChapas", "QtdChapas")) or (str(len(slab_rows)) if slab_rows else "")
    status_flag = _strip_html(_g(rec, "displayProduct"))

    price1 = _strip_html(_g(rec, "preco1", "precoPrincipal"))
    price2 = _strip_html(_g(rec, "preco2", "precoSecundario"))

    detail_url = f"{origin}{DETAIL_PATH}?{DETAIL_ID_PARAM}={bundle_id}" if not bundle_id.startswith("row") else ""

    return {
        "product_id":       bundle_id,
        "material":         _scalar(_g(rec, "nomeMaterial", "Material")).strip(),
        "stone_type":       _scalar(_g(rec, "nomeComposicao", "Composicao")),
        "color":            _scalar(_g(rec, "cor", "Cor", "nomeCor")),
        "finish":           _scalar(_g(rec, "acabamento", "Acabamento")),
        "quality":          _scalar(_g(rec, "nomeQualidade", "Qualidade")),
        "classification":   _scalar(_g(rec, "classificacao", "Classificacao")),
        "thickness":        _scalar(_g(rec, "nomeEspessura", "Espessura")),
        "block":            _scalar(_g(rec, "bloco", "Bloco")),
        "bundle_no":        _scalar(_g(rec, "cavalete", "Cavalete")),
        "slab_numbers":     _scalar(_g(rec, "chapas") if not isinstance(_g(rec, "chapas"), list) else "")
                            or " - ".join(s["n"] for s in slab_rows if s.get("n")),
        "slab_count":       slab_count,
        "dimension_height": first_slab.get("h_cm", ""),
        "dimension_length": first_slab.get("w_cm", ""),
        "height_in":        first_slab.get("h_in", ""),
        "length_in":        first_slab.get("w_in", ""),
        "average_size":     _scalar(_g(rec, "averageSize", "AverageSize")),
        "area_sqft":        _scalar(_g(rec, "totalSqft", "TotalSqft")),
        "area_sqmt":        _scalar(_g(rec, "totalSqmt", "TotalSqmt")),
        "weight":           _scalar(_g(rec, "peso", "Peso")),
        "origin":           _scalar(_g(rec, "nomePais", "Pais")),
        "country_code":     _scalar(_g(rec, "siglaPais", "SiglaPais")),
        "location":         _scalar(_g(rec, "localizacao", "Localizacao")),
        "price1":           price1,
        "price2":           price2,
        "currency":         _scalar(_g(rec, "moeda", "Moeda")),
        "status_flag":      status_flag,
        "detail_url":       detail_url,
        "slabs_detail":     json.dumps(slab_rows, ensure_ascii=False) if slab_rows else "",
        "image_count":      len(images),
        "image_urls":       " | ".join(images),
        "raw_json":         json.dumps(rec, ensure_ascii=False),
    }


# ============================================================================
# BROWSER FLOW
# ============================================================================

def _dismiss_overlays(page):
    """Best-effort close of cookie banner / welcome / OOS modals."""
    clickers = [
        "text=Got It",
        "text=GOT IT",
        "#ctl00_AceitarCookiesLinkButton",
        "button:has-text('Accept')",
        ".close",
        "[class*='close']",
    ]
    for sel in clickers:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=1500)
                logger.debug("Dismissed overlay via %s", sel)
                page.wait_for_timeout(400)
        except Exception:
            pass
    # Welcome popup often closes on Escape
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


def _count_cards(page, selector):
    """Count only VISIBLE matches that aren't hidden detail/template nodes."""
    if not selector:
        return 0
    try:
        return page.evaluate(_JS_COUNT_VISIBLE, selector) or 0
    except Exception:
        # Fallback to a plain count if the JS path fails for any reason.
        try:
            return page.locator(selector).count()
        except Exception:
            return 0


def _best_selector(page):
    """Pick the CARD_SELECTOR that yields the most VISIBLE elements (layer b)."""
    best, best_n = None, 0
    for sel in CARD_SELECTORS:
        n = _count_cards(page, sel)
        if n > best_n:
            best, best_n = sel, n
    return best, best_n


def _scroll_to_load_all(page, selector):
    """Scroll until the card count stabilises (lazy infinite-scroll grid)."""
    stable = 0
    last = -1
    for rnd in range(SCROLL_MAX_ROUNDS):
        n = _count_cards(page, selector) if selector else 0
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        _sleep_jitter(SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX)
        # nudge for grids that listen to scroll events mid-page
        page.evaluate("window.scrollBy(0, -250);")
        new_n = _count_cards(page, selector) if selector else 0
        logger.info("  scroll %d: %d cards", rnd + 1, new_n)
        if new_n == last:
            stable += 1
            if stable >= SCROLL_STABLE_ROUNDS:
                logger.info("  card count stable at %d, stopping scroll.", new_n)
                break
        else:
            stable = 0
        last = new_n
    return _count_cards(page, selector) if selector else 0


def _prime_scroll(page, selector, rounds):
    """A few quick scrolls just to trigger ObterListaBundles requests so we can
    detect pagination, before replaying the endpoint in full."""
    for rnd in range(rounds):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        _sleep_jitter(SCROLL_PAUSE_MIN, SCROLL_PAUSE_MAX)
        page.evaluate("window.scrollBy(0, -250);")
        n = _count_cards(page, selector) if selector else 0
        logger.info("  prime scroll %d: %d cards, %d bundle request(s) seen",
                    rnd + 1, n, len(_bundle_requests))
        if len(_bundle_requests) >= 2:
            break


# -- JS extractors (run in-page) ---------------------------------------------

# Shared JS prelude: absolute-URL helper, visibility test, and an isCard()
# predicate that rejects hidden nodes and template/detail/modal containers.
# The exclude list is baked in from EXCLUDE_CLASS_SUBSTRINGS.
_JS_HELPERS = (
    "const EXCL = " + json.dumps([s.lower() for s in EXCLUDE_CLASS_SUBSTRINGS]) + ";"
    "const absol = u => { try { return new URL(u, location.href).href; } catch { return u; } };"
    "const isVisible = el => {"
    "  if (!el || el.offsetParent === null) return false;"
    "  const r = el.getBoundingClientRect();"
    "  if (r.width < 2 || r.height < 2) return false;"
    "  const st = getComputedStyle(el);"
    "  return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';"
    "};"
    "const isExcluded = el => { const c = (el.className && el.className.toString ? el.className.toString() : '').toLowerCase();"
    "  return EXCL.some(x => c.indexOf(x) !== -1); };"
    "const isCard = el => isVisible(el) && !isExcluded(el);"
    "const extractCard = card => {"
    "  const imgs = Array.from(card.querySelectorAll('img'))"
    "    .map(i => absol(i.getAttribute('data-src') || i.getAttribute('data-original') || i.getAttribute('data-lazy') || i.src))"
    "    .filter(Boolean);"
    "  const data = {};"
    "  const collect = node => { for (const a of node.attributes||[]) { if (a.name.startsWith('data-')) data[a.name]=a.value; } };"
    "  collect(card); card.querySelectorAll('*').forEach(collect);"
    "  const links = Array.from(card.querySelectorAll('a')).map(a=>a.getAttribute('href'))"
    "    .filter(h=>h && h!=='#' && !h.startsWith('javascript'));"
    "  return { text:(card.innerText||'').trim(), images:Array.from(new Set(imgs)), data:data, links:links, html_id:card.id||'' };"
    "};"
)

# Count visible, non-template matches for a given selector.
_JS_COUNT_VISIBLE = (
    "(selector) => {" + _JS_HELPERS +
    "  return Array.from(document.querySelectorAll(selector)).filter(isCard).length;"
    "}"
)

# Generic fallback: largest cluster of repeated sibling nodes each containing an
# <img>, filtered to visible non-template cards.
JS_GENERIC_EXTRACT = (
    "(() => {" + _JS_HELPERS +
    "  const groups = new Map();"
    "  document.querySelectorAll('img').forEach(img => {"
    "    let el = img;"
    "    for (let up = 0; up < 4 && el.parentElement; up++) el = el.parentElement;"
    "    const parent = el.parentElement; if (!parent) return;"
    "    if (!groups.has(parent)) groups.set(parent, new Set());"
    "    groups.get(parent).add(el);"
    "  });"
    "  let bestParent = null, bestCount = 0;"
    "  for (const [parent, kids] of groups) { if (kids.size > bestCount) { bestCount = kids.size; bestParent = parent; } }"
    "  if (!bestParent || bestCount < 2) return [];"
    "  return Array.from(bestParent.children).filter(isCard).map(extractCard);"
    "})()"
)


def _js_extract_selector(selector):
    """Build a JS extractor for a specific selector (layer b), visible-only."""
    sel = json.dumps(selector)
    return (
        "(() => {" + _JS_HELPERS +
        f"  const cards = Array.from(document.querySelectorAll({sel})).filter(isCard);"
        "  return cards.map(extractCard);"
        "})()"
    )


# ============================================================================
# PARSING raw card -> structured row
# ============================================================================

RE_THICKNESS = re.compile(r"(\d+(?:\.\d+)?)\s?(cm|mm|in|\")", re.I)
RE_DIMENSION = re.compile(r"(\d+(?:[.,]\d+)?)\s?[xX×]\s?(\d+(?:[.,]\d+)?)")
RE_PRICE = re.compile(r"(?:US?\$|\$|€|£)\s?([\d,]+(?:\.\d{1,2})?)")
RE_LOT = re.compile(r"(?:lot|lote|bundle|bnd|block|bloco|slab)\s*#?\s*([A-Za-z0-9\-/]+)", re.I)
RE_AREA = re.compile(r"(\d+(?:[.,]\d+)?)\s?(m2|m²|sqft|sf|ft2|ft²)", re.I)


def _first(d, *keys):
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return ""


def parse_card(raw, idx):
    """Turn a raw extracted card dict into a flat row dict."""
    text = clean_text(raw.get("text", ""))
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    data = raw.get("data", {}) or {}
    # Dedupe images while preserving first-seen order.
    images = list(dict.fromkeys(raw.get("images", []) or []))
    links = raw.get("links", []) or []

    # Product id: prefer a data-* id, else an id in a link, else the html id,
    # else a positional fallback.
    product_id = (
        _first(data, "data-id", "data-bundleid", "data-bundle-id",
               "data-materialid", "data-material-id", "data-codigo", "data-cod")
        or raw.get("html_id", "")
    )
    if not product_id:
        for href in links:
            m = re.search(r"[?&](?:id|bundle|material|cod)=([^&]+)", href, re.I)
            if m:
                product_id = m.group(1)
                break
    if not product_id:
        product_id = f"row{idx:05d}"

    # Material name: first non-numeric, non-price line is the usual title.
    material = _first(data, "data-material", "data-nome", "data-name")
    if not material:
        for ln in lines:
            if not RE_PRICE.search(ln) and not ln.replace(".", "").isdigit():
                material = ln
                break
    # Single-line card fallback: take the leading words before the first
    # number/thickness/price token on the first line.
    if not material and lines:
        m = re.match(r"^([A-Za-z][A-Za-z &'\-]+?)(?=\s*(?:\d|US?\$|\$|€|£))", lines[0])
        if m:
            material = m.group(1).strip()

    th = RE_THICKNESS.search(text)
    thickness = (th.group(1) + th.group(2).lower()) if th else _first(
        data, "data-thickness", "data-espessura")

    dm = RE_DIMENSION.search(text)
    dim_h = dm.group(1).replace(",", ".") if dm else _first(data, "data-height", "data-altura")
    dim_l = dm.group(2).replace(",", ".") if dm else _first(data, "data-length", "data-comprimento")

    pm = RE_PRICE.search(text)
    price = pm.group(1) if pm else _first(data, "data-price", "data-preco", "data-valor")

    lm = RE_LOT.search(text)
    lot = lm.group(1) if lm else _first(data, "data-lot", "data-lote", "data-bundle", "data-bloco")

    am = RE_AREA.search(text)
    area = (am.group(1).replace(",", ".") + am.group(2).lower()) if am else ""

    detail_url = links[0] if links else ""

    return {
        "product_id": product_id,
        "material": material,
        "stone_type": _first(data, "data-composicao", "data-stonetype", "data-stone-type"),
        "color": _first(data, "data-color", "data-cor"),
        "finish": _first(data, "data-finish", "data-acabamento"),
        "quality": _first(data, "data-quality", "data-qualidade"),
        "type": _first(data, "data-type", "data-tipo"),
        "origin": _first(data, "data-origin", "data-origem"),
        "location": _first(data, "data-location", "data-local", "data-localizacao"),
        "thickness": thickness,
        "lot_bundle": lot,
        "dimension_height": dim_h,
        "dimension_length": dim_l,
        "area": area,
        "price": price,
        "currency": "",
        "detail_url": detail_url,
        "all_data_attrs": " ; ".join(f"{k}={v}" for k, v in sorted(data.items())),
        "raw_text": " | ".join(lines),
        "image_count": len(images),
        "image_urls": " | ".join(images),
    }


# ============================================================================
# DETAIL OPEN (optional, layer for full-res images)
# ============================================================================

JS_COLLECT_VISIBLE_IMAGES = r"""
() => {
  const absol = u => { try { return new URL(u, location.href).href; } catch { return u; } };
  // Grab large/visible images that look like slab photos (skip icons/logos).
  return Array.from(document.querySelectorAll('img'))
    .filter(i => (i.naturalWidth||i.width||0) >= 200)
    .map(i => absol(i.getAttribute('data-src') || i.getAttribute('data-original') || i.src))
    .filter(Boolean);
}
"""


def _visible_card_handles(page, selector):
    """Return JS handles for the visible, non-template cards in DOM order."""
    try:
        return page.evaluate_handle(
            "(selector) => {" + _JS_HELPERS +
            "  return Array.from(document.querySelectorAll(selector)).filter(isCard);"
            "}", selector
        )
    except Exception:
        return None


def open_card_details(page, card_handle, idx):
    """Open one card (by element handle), collect any larger images, then close."""
    try:
        card_handle.scroll_into_view_if_needed(timeout=3000)
        try:
            card_handle.click(timeout=3000)
        except Exception:
            # Fall back to a JS click for elements Playwright deems unstable.
            page.evaluate("(el) => el.click()", card_handle)
        page.wait_for_timeout(int(random.uniform(DETAIL_DELAY_MIN, DETAIL_DELAY_MAX) * 1000))
        imgs = page.evaluate(JS_COLLECT_VISIBLE_IMAGES) or []
        _dismiss_overlays(page)
        return imgs
    except Exception as e:
        record_failure("detail_open", product_id=f"idx{idx}", error=str(e).splitlines()[0])
        return []


# ============================================================================
# IMAGE DOWNLOAD (requests, using browser cookies)
# ============================================================================

def build_image_session(context, base_url):
    """A requests.Session seeded with the browser's cookies for gated images."""
    sess = requests.Session()
    try:
        for c in context.cookies():
            sess.cookies.set(c.get("name"), c.get("value"),
                             domain=c.get("domain"), path=c.get("path", "/"))
    except Exception:
        pass
    sess.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": base_url,
        "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    })
    return sess


def download_image(sess, url, filepath, product_id, label):
    try:
        _sleep_jitter(IMAGE_DELAY_MIN, IMAGE_DELAY_MAX)
        r = sess.get(url, timeout=IMAGE_TIMEOUT, allow_redirects=True)
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else RATE_LIMIT_BACKOFF
            logger.warning("HTTP 429 on %s. Sleeping %.0fs...", filepath.name, wait)
            time.sleep(wait)
            r = sess.get(url, timeout=IMAGE_TIMEOUT, allow_redirects=True)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        ct = r.headers.get("content-type", "")
        if "image" not in ct.lower():
            raise RuntimeError(f"Non-image content-type: {ct}")
        with open(filepath, "wb") as f:
            f.write(r.content)
        logger.debug("Image saved: %s (%d bytes, %s)", filepath.name, len(r.content), label)
        return True
    except Exception as e:
        record_failure("image", product_id=product_id, url=url,
                       status=getattr(getattr(e, "response", None), "status_code", None),
                       error=str(e))
        return False


def download_product_images(sess, product_id, image_urls):
    if not image_urls:
        return ([], 0)
    safe_id = clean_id(product_id)
    saved = []
    for i, url in enumerate(image_urls, start=1):
        ext = ".jpg"
        last = url.rsplit("/", 1)[-1] if "/" in url else url
        if "." in last:
            cand = "." + last.rsplit(".", 1)[1].split("?")[0].lower()
            if len(cand) <= 5:
                ext = cand
        filename = f"{safe_id}_{i}{ext}"
        filepath = IMAGES_DIR / filename
        if filepath.exists() and filepath.stat().st_size > 0:
            saved.append(filename)
            continue
        if download_image(sess, url, filepath, product_id, f"image {i}"):
            saved.append(filename)
    return (saved, len(saved))


# ============================================================================
# CSV
# ============================================================================

CSV_FIELDS = [
    "product_id", "material", "stone_type", "color", "finish", "quality",
    "classification", "thickness", "block", "bundle_no",
    "slab_numbers", "slab_count",
    "dimension_height", "dimension_length", "height_in", "length_in",
    "average_size", "area_sqft", "area_sqmt", "weight",
    "origin", "country_code", "location",
    "price1", "price2", "currency", "status_flag",
    "detail_url", "slabs_detail",
    "image_count", "image_urls", "image_filenames_local",
    "raw_json", "scrape_timestamp",
]


def open_csv():
    fh = open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig", buffering=1)
    writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    fh.flush()
    return fh, writer


def append_row(fh, writer, row):
    writer.writerow(row)
    fh.flush()


# ============================================================================
# MAIN
# ============================================================================

def build_url(base_url, view):
    if not view:
        return base_url
    # Append V=view without clobbering the S= token.
    sep = "&" if "?" in base_url else "?"
    if re.search(r"[?&]V=", base_url):
        return base_url
    return f"{base_url}{sep}V={view}"


def main():
    ap = argparse.ArgumentParser(description="SlabWare / Polonine inventory scraper")
    ap.add_argument("--url", default=DEFAULT_URL, help="FullInventory URL incl. ?S= token")
    ap.add_argument("--view", default=VIEW, help="photo | list | (empty)")
    ap.add_argument("--headful", action="store_true", help="show the browser")
    ap.add_argument("--details", action="store_true",
                    help="open each card for extra full-res images (slow)")
    ap.add_argument("--no-details", action="store_true",
                    help="force-skip per-card detail open (default is already off)")
    ap.add_argument("--no-images", action="store_true", help="skip image downloads")
    ap.add_argument("--no-enrich", action="store_true",
                    help="skip per-product Product-Details enrichment")
    ap.add_argument("--selector", default=None, help="force a specific card CSS selector")
    args = ap.parse_args()

    headless = HEADLESS and not args.headful
    open_details = (OPEN_DETAILS or args.details) and not args.no_details
    do_images = DOWNLOAD_IMAGES and not args.no_images
    enrich = ENRICH_DETAILS and not args.no_enrich

    url = build_url(args.url, args.view)
    parsed = urlparse(args.url)
    base_origin = f"{parsed.scheme}://{parsed.netloc}"

    IMAGES_DIR.mkdir(exist_ok=True)
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    logger.info("=" * 70)
    logger.info("POLONINE / SLABWARE INVENTORY SCRAPER")
    logger.info("=" * 70)
    logger.info("Started:  %s", started)
    logger.info("Target:   %s", url[:90] + ("..." if len(url) > 90 else ""))
    logger.info("View:     %s", args.view or "(default)")
    logger.info("Headless: %s | details: %s | images: %s", headless, open_details, do_images)
    logger.info("CSV out:  %s", OUTPUT_CSV)
    logger.info("Images:   %s", IMAGES_DIR)
    logger.info("Log file: %s", LOG_FILE)
    logger.info("")

    rows = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1440, "height": 1000},
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        _attach_sniffer(page)

        logger.info("Loading page...")
        try:
            page.goto(url, wait_until="domcontentloaded")
        except PWTimeout:
            logger.warning("Initial navigation timed out; continuing anyway.")
        page.wait_for_timeout(PAGE_SETTLE_MS)
        _dismiss_overlays(page)

        # Wait for the grid to populate past the AJAX spinner.
        logger.info("Waiting for inventory grid to render...")
        selector = args.selector
        if not selector:
            deadline = time.time() + GRID_WAIT_MS / 1000
            while time.time() < deadline:
                sel, n = _best_selector(page)
                if sel and n >= 2:
                    selector = sel
                    logger.info("Detected card selector %r (%d cards so far).", sel, n)
                    break
                page.wait_for_timeout(1000)
        if not selector:
            logger.warning("No known card selector matched; will use generic fallback.")

        # Prime: a few quick scrolls to trigger ObterListaBundles requests so we
        # can detect the pagination scheme, then replay the endpoint in full.
        _prime_scroll(page, selector, PRIME_SCROLL_ROUNDS)

        DEBUG_DIR.mkdir(exist_ok=True)
        index = _dump_captured()

        # ---- PRIMARY: replay the bundle endpoint (reliable) + merge any passive
        # JSON we happened to capture. Replay walks every page itself, so we do
        # NOT need to scroll the whole grid.
        replay_bundles = fetch_all_bundles_via_replay(page)
        passive_bundles = collect_api_bundles(index)
        api_bundles = _merge_bundles(replay_bundles, passive_bundles)

        # If replay found nothing, fall back to a full scroll (which fires every
        # page through the browser) and try once more.
        if not api_bundles:
            logger.info("No bundles from priming; doing a full scroll and retrying...")
            _scroll_to_load_all(page, selector or "img")
            index = _dump_captured()
            api_bundles = _merge_bundles(
                fetch_all_bundles_via_replay(page),
                collect_api_bundles(index))

        cards_seen = _count_cards(page, selector) if selector else 0

        # Debug snapshot.
        try:
            (DEBUG_DIR / "rendered_dom.html").write_text(
                page.content(), encoding="utf-8", errors="ignore")
            page.screenshot(path=str(DEBUG_DIR / "page.png"), full_page=True)
        except Exception as e:
            logger.debug("Debug dump failed: %s", e)

        if api_bundles:
            logger.info("Using API data: %d bundles from %s.",
                        len(api_bundles), BUNDLE_ENDPOINT_HINT)
            try:
                (DEBUG_DIR / "first_bundle.json").write_text(
                    json.dumps(api_bundles[0], indent=2, ensure_ascii=False),
                    encoding="utf-8")
                logger.info("Bundle JSON keys: %s",
                            ", ".join(list(api_bundles[0].keys())[:40]))
            except Exception:
                pass
            if cards_seen and len(api_bundles) < cards_seen * 0.8:
                logger.warning("API bundles (%d) < cards on page (%d); some pages "
                               "may have been rate-limited. Rerun if short.",
                               len(api_bundles), cards_seen)

            # Enrich each bundle with its Product-Details data (full attributes
            # and all slab photos) before flattening to rows.
            if enrich:
                api_bundles = enrich_with_details(page, api_bundles, base_origin)

            for i, b in enumerate(api_bundles):
                rows.append(parse_bundle(b, i, base_origin))
        else:
            # ---- LAST RESORT: scrape the rendered DOM cards (layer b/c), with
            # promo/section widgets filtered out.
            logger.info("No API bundles available; falling back to DOM extraction.")
            if selector:
                raw_cards = page.evaluate(_js_extract_selector(selector)) or []
            else:
                raw_cards = page.evaluate(JS_GENERIC_EXTRACT) or []
            logger.info("Extracted %d raw cards (pre-filter).", len(raw_cards))

            if open_details and selector and raw_cards:
                logger.info("Opening %d cards for full-res images...", len(raw_cards))
                handle = _visible_card_handles(page, selector)
                try:
                    props = handle.get_properties() if handle else {}
                    card_handles = [v.as_element() for v in props.values()]
                    card_handles = [c for c in card_handles if c is not None]
                except Exception:
                    card_handles = []
                for i, ch in enumerate(card_handles):
                    if i >= len(raw_cards):
                        break
                    extra = open_card_details(page, ch, i)
                    if extra:
                        merged = list(dict.fromkeys((raw_cards[i].get("images") or []) + extra))
                        raw_cards[i]["images"] = merged

            kept = 0
            for i, raw in enumerate(raw_cards):
                row = parse_card(raw, i)
                if _norm(row.get("material", "")) in _SECTION_LABELS:
                    continue  # skip "NEW ARRIVALS"/"RECOMMENDED"/etc. promo cards
                rows.append(row)
                kept += 1
            logger.info("Kept %d cards after dropping promo/section widgets.", kept)

        # Image session reuses the browser cookies.
        img_sess = build_image_session(context, base_origin) if do_images else None

        fh, writer = open_csv()
        written = 0
        interrupted = False
        try:
            for idx, row in enumerate(rows, start=1):
                row["scrape_timestamp"] = started
                logger.info("  [%d/%d] %s - %s (%s | blk %s | %dimg)",
                            idx, len(rows), row["product_id"],
                            (row.get("material") or "?")[:36],
                            row.get("thickness") or "?",
                            row.get("block") or "?",
                            row.get("image_count") or 0)
                if do_images:
                    image_urls = [u.strip() for u in (row["image_urls"] or "").split("|") if u.strip()]
                    local_files, _ = download_product_images(img_sess, row["product_id"], image_urls)
                    row["image_filenames_local"] = " | ".join(local_files)
                append_row(fh, writer, row)
                written += 1
                if (do_images and LONG_BREAK_EVERY_N_PRODUCTS
                        and idx % LONG_BREAK_EVERY_N_PRODUCTS == 0 and idx < len(rows)):
                    _sleep_jitter(LONG_BREAK_MIN, LONG_BREAK_MAX, label="long break")
        except KeyboardInterrupt:
            interrupted = True
            logger.warning("\nInterrupted by user. Saving partial results...")
        finally:
            fh.close()
            write_failures_csv()

        context.close()
        browser.close()

    finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    imgs_on_disk = len(list(IMAGES_DIR.glob("*"))) if IMAGES_DIR.exists() else 0
    image_failures = sum(1 for f in _failures if f["kind"] == "image")

    logger.info("")
    logger.info("=" * 70)
    logger.info("DONE" + (" (interrupted)" if 'interrupted' in dir() and interrupted else ""))
    logger.info("=" * 70)
    logger.info("Started:        %s", started)
    logger.info("Finished:       %s", finished)
    logger.info("Rows:           %d written to CSV", len(rows))
    logger.info("Images:         %d files on disk in %s", imgs_on_disk, IMAGES_DIR)
    logger.info("Image failures: %d", image_failures)
    logger.info("CSV:            %s", OUTPUT_CSV)
    logger.info("Log file:       %s", LOG_FILE)
    logger.info("Debug dir:      %s  (check rendered_dom.html if rows look empty)", DEBUG_DIR)
    if _failures:
        logger.info("Failures CSV:   %s", FAILURES_CSV)


if __name__ == "__main__":
    main()