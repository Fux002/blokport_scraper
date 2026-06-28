"""Bruma Granite scraper, migrated onto ScraperBase.

Same SlabWare SaaS platform as Zucchi: a Cloudflare-fronted ASP.NET WebForms
tenant with two WebMethods, ObterListaBundles (paginated bundle listing) and
DetalheBundle (per-bundle enrichment). The site-specific bits here are:

  - a Cloudflare warm-up GET to seed cookies, then a JSON POST pagination over
    ObterListaBundles (40 bundles per `inicio` offset);
  - a per-bundle DetalheBundle POST that enriches each listing row with
    classification, colour, per-slab dimensions and the full fotos[] photo list;
  - building full source photo URLs from the fotos[] paths.

Because Cloudflare gates this tenant on the TLS fingerprint, the network calls
use curl_cffi (Chrome impersonation) rather than the base httpx client. curl_cffi
is imported lazily inside run-time methods so the module still imports (and the
scraper class is inspectable) without it installed; install with:
    pip3 install curl_cffi

Bruma sells slab inventory (one row per bundle of slabs), so the format is the
constant `slab` (item 3), exactly like Zucchi.

Prices are gated behind SlabWare's login (Logado: false -> 'Login for Price'
HTML stubs); the public scrape blanks those.

Run:  python scrapers/brumagran.py
"""

from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/brumagran.py` and `python -m scrapers.brumagran`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

# --- Site config ------------------------------------------------------------
# The S= token from the URL. SlabWare accepts an empty json filter regardless of
# the token value, but a fresh one is used to be safe.
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

# Server-controlled, confirmed via probe.
PAGE_SIZE = 40

# Speed / politeness (delays BEFORE each call).
PAGE_DELAY_MIN, PAGE_DELAY_MAX = 3.0, 6.0
DETAIL_DELAY_MIN, DETAIL_DELAY_MAX = 1.5, 3.5
LONG_BREAK_EVERY_N_PAGES = 15
LONG_BREAK_MIN, LONG_BREAK_MAX = 30.0, 90.0

RATE_LIMIT_BACKOFF = 60.0
IMPERSONATE = "chrome120"

API_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=utf-8",
    "origin": BASE,
    "referer": PAGE_URL,
    "x-requested-with": "XMLHttpRequest",
}

DISPLAY_RE = re.compile(r"class='([^']+)'", re.IGNORECASE)
# Prices are returned as '<a>Login for Price</a>' stubs when unauthenticated.
_LOGIN_STUB_RE = re.compile(r"<\s*a\s|login\s*for\s*price", re.IGNORECASE)


def _get(d: dict, key: str, default: str = "") -> Any:
    v = d.get(key)
    if v is None or v == "":
        return default
    return v


def parse_display_status(html: str) -> str:
    """The displayProduct field is a tiny HTML span; extract a clean status."""
    if not html:
        return ""
    m = DISPLAY_RE.search(html)
    if not m:
        return ""
    classes = m.group(1).lower()
    if "recommended-product" in classes and "display: block" in html.lower():
        return "recommended"
    if "new-product" in classes and "display: block" in html.lower():
        return "new"
    return ""


def clean_price(value: Any) -> str:
    """Return '' if the value is an HTML 'Login for Price' stub, else as-is."""
    if not value:
        return ""
    s = str(value)
    if _LOGIN_STUB_RE.search(s):
        return ""
    return s


def join_slabs(chapas: list, key: str) -> str:
    """Pipe-join a single field across all slabs, e.g. all widths."""
    if not chapas:
        return ""
    return " | ".join(str(c.get(key, "") or "") for c in chapas)


def photo_urls_from_fotos(fotos: list) -> list[str]:
    """Convert the fotos[] array of paths into a list of full source URLs."""
    out: list[str] = []
    for path in fotos or []:
        if not path:
            continue
        if path.startswith("http"):
            out.append(path)
        elif path.startswith("/"):
            out.append(BASE + path)
        else:
            out.append(BASE + "/" + path)
    return out


class BrumagranScraper(ScraperBase):
    source = "brumagran"
    category = "slab"        # Bruma is slab inventory, one row per bundle (item 3)
    id_field = "bundle_id"   # images named brumagran_<bundle_id>_<idx>
    use_curl_cffi = True   # Cloudflare-fronted SlabWare tenant

    columns = [
        # Core identity
        "bundle_id", "material_name", "composition", "block", "bundle_ref",
        # Material spec
        "finish", "thickness", "quality", "classification", "color",
        # Inventory state
        "slab_count", "slabs", "slab_count_actual", "slabs_available",
        "display_status", "location", "country_code", "country",
        # Dimensions / area
        "average_size", "avg_size_metric", "avg_size_imperial",
        "total_sqft", "total_sqmt", "total_area_summary",
        # Per-slab arrays (pipe-separated)
        "slab_ids", "slab_numbers",
        "slab_widths_m", "slab_heights_m",
        "slab_lengths_in", "slab_heights_in",
        "slab_areas_sqmt", "slab_areas_sqft",
        # Pricing (empty until authenticated)
        "currency",
        "price_main", "price_main_old",
        "price_secondary", "price_secondary_old",
        "price_total_bundle",
        "price_1", "price_old_1", "price_2", "price_old_2",
        # Description, video, links
        "description", "video_url", "kitchen_visualizer_url",
        # Image references (source-side)
        "image_filename_remote", "image_url_full", "image_url_thumb", "photo_count",
    ]

    # --- HTTP via curl_cffi (Cloudflare TLS impersonation) -------------------
    def _ensure_session(self):
        """Lazily build a curl_cffi session and warm it up against Cloudflare.

        Imported lazily so the module imports without curl_cffi installed; the
        network only happens at run() time.
        """
        sess = getattr(self, "_cf_session", None)
        if sess is not None:
            return sess
        try:
            from curl_cffi import requests as cf_requests
        except ImportError as exc:  # pragma: no cover - runtime requirement
            raise RuntimeError(
                "brumagran needs curl_cffi for Cloudflare; install with "
                "`pip3 install curl_cffi`"
            ) from exc
        sess = cf_requests.Session(impersonate=IMPERSONATE)
        self._cf_session = sess
        self.log.info("warming up session: GET %s...", PAGE_URL[:80])
        r = sess.get(PAGE_URL, timeout=self.timeout)
        self.log.info("warm-up status %d, cookies %s", r.status_code,
                      list(sess.cookies.keys()))
        if r.status_code != 200:
            raise RuntimeError(f"warm-up GET failed: {r.status_code}")
        return sess

    def _api_post(self, url: str, payload: dict, label: str, critical: bool = False) -> dict:
        """POST a SlabWare WebMethod with retry, backoff and 429 handling.

        Returns the inner `d` object, or {} after exhausting retries.
        """
        sess = self._ensure_session()
        last_err = None
        for attempt in range(self.max_retries):
            try:
                r = sess.post(url, headers=API_HEADERS,
                              data=json.dumps(payload), timeout=self.timeout)
                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else RATE_LIMIT_BACKOFF
                    self.log.warning("HTTP 429 on %s. Sleeping %.0fs...", label, wait)
                    time.sleep(wait)
                    continue
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                return r.json().get("d") or {}
            except Exception as e:
                last_err = e
                wait = self.backoff_base ** attempt
                self.log.warning("%s attempt %d/%d failed: %s. Waiting %.0fs...",
                                 label, attempt + 1, self.max_retries, e, wait)
                time.sleep(wait)
        self.record_failure("http", url=url, error=str(last_err), label=label)
        # Only a LISTING fetch (critical=True) failing means truncation -- its empty result is read as
        # end-of-pagination, so mark the run INCOMPLETE to keep the prior complete folder. A DETAIL fetch
        # failing just leaves that one row's enrichment blank (the row still emits); do NOT discard the
        # whole fully-paginated scrape for one transient detail miss.
        if critical:
            self.mark_incomplete(f"{label} fetch failed after {self.max_retries} retries")
        return {}

    @staticmethod
    def _sleep_jitter(low: float, high: float) -> None:
        time.sleep(random.uniform(low, high))

    # --- listing -------------------------------------------------------------
    def list_products(self) -> Iterable[Any]:
        """Paginate ObterListaBundles over the `inicio` offset, yielding raw
        listing bundle dicts. Enrichment happens in parse_product."""
        # First batch (inicio=0). critical=True: a listing-fetch failure means truncation.
        d = self._api_post(API_URL, {"inicio": 0, "json": ""}, "listing inicio=0", critical=True)
        first_batch = json.loads(d.get("Bundles") or "[]")
        if not first_batch:
            self.log.error("first batch empty; aborting")
            return
        self.log.info("listing: page size %d, long break every %d pages",
                      PAGE_SIZE, LONG_BREAK_EVERY_N_PAGES)
        yield from first_batch

        inicio = PAGE_SIZE
        page_idx = 1
        while True:
            if LONG_BREAK_EVERY_N_PAGES and page_idx % LONG_BREAK_EVERY_N_PAGES == 0:
                self._sleep_jitter(LONG_BREAK_MIN, LONG_BREAK_MAX)
            else:
                self._sleep_jitter(PAGE_DELAY_MIN, PAGE_DELAY_MAX)

            d = self._api_post(API_URL, {"inicio": inicio, "json": ""},
                               f"listing inicio={inicio}", critical=True)
            batch = json.loads(d.get("Bundles") or "[]")
            if not batch:
                self.log.info("empty batch at inicio=%d; done paginating", inicio)
                break
            yield from batch
            inicio += PAGE_SIZE
            page_idx += 1

    def _fetch_detail(self, bundle_id: Any) -> dict:
        """Call DetalheBundle for one bundle, returning the inner Bundle dict."""
        d = self._api_post(DETAIL_API_URL,
                           {"IdBundle": bundle_id, "IdCampanha": 0},
                           f"detail bundle={bundle_id}")
        return d.get("Bundle") or {}

    # --- parsing -------------------------------------------------------------
    def parse_product(self, item: dict) -> Optional[dict]:
        bundle_id = _get(item, "id")
        row = {
            # From listing
            "bundle_id": bundle_id,
            "material_name": _get(item, "nomeMaterial"),
            "composition": _get(item, "nomeComposicao"),
            "block": _get(item, "bloco"),
            "bundle_ref": _get(item, "cavalete"),
            "finish": _get(item, "acabamento"),
            "slab_count": _get(item, "qtdChapas"),
            "slabs": _get(item, "chapas"),
            "average_size": _get(item, "averageSize"),
            "quality": _get(item, "nomeQualidade"),
            "thickness": _get(item, "nomeEspessura"),
            "price_1": _get(item, "preco1"),
            "price_old_1": _get(item, "precoAntigo1"),
            "price_2": _get(item, "preco2"),
            "price_old_2": _get(item, "precoAntigo2"),
            "display_status": parse_display_status(item.get("displayProduct", "")),
            "country_code": _get(item, "siglaPais"),
            "country": _get(item, "nomePais"),
            "image_filename_remote": item.get("fotoPrincipal") or "",
            # Filled by detail merge below.
            "classification": "", "color": "", "location": "", "currency": "",
            "description": "", "video_url": "", "slabs_available": "",
            "total_area_summary": "", "avg_size_metric": "", "avg_size_imperial": "",
            "total_sqft": "", "total_sqmt": "", "kitchen_visualizer_url": "",
            "price_main": "", "price_main_old": "", "price_secondary": "",
            "price_secondary_old": "", "price_total_bundle": "",
            "slab_ids": "", "slab_numbers": "", "slab_widths_m": "",
            "slab_heights_m": "", "slab_lengths_in": "", "slab_heights_in": "",
            "slab_areas_sqmt": "", "slab_areas_sqft": "", "slab_count_actual": "",
            "image_url_full": "", "image_url_thumb": "", "photo_count": 0,
        }

        # Per-bundle detail enrichment (DetalheBundle).
        self._sleep_jitter(DETAIL_DELAY_MIN, DETAIL_DELAY_MAX)
        detail = self._fetch_detail(bundle_id)

        photo_urls: list[str] = []
        if detail:
            fotos = detail.get("fotos") or []
            chapas = detail.get("chapas") or []
            photo_urls = photo_urls_from_fotos(fotos)
            link = detail.get("linkProduto") or ""
            row.update({
                "classification": _get(detail, "classificacao"),
                "color": _get(detail, "cor"),
                "location": _get(detail, "localizacao"),
                "currency": _get(detail, "moeda"),
                "description": _get(detail, "observacao"),
                "video_url": _get(detail, "video"),
                "slabs_available": _get(detail, "slabsAvailable"),
                "total_area_summary": _get(detail, "totalValoresMaterialSlabs"),
                "avg_size_metric": _get(detail, "averageSize"),
                "avg_size_imperial": _get(detail, "averageSizeSecundario"),
                "total_sqft": _get(detail, "totalSqft"),
                "total_sqmt": _get(detail, "totalSqmt"),
                "kitchen_visualizer_url": BASE + link if link.startswith("/")
                    else _get(detail, "linkProduto"),
                "price_main": clean_price(detail.get("precoPrincipal")),
                "price_main_old": clean_price(detail.get("precoAntigoPrincipal")),
                "price_secondary": clean_price(detail.get("precoSecundario")),
                "price_secondary_old": clean_price(detail.get("precoAntigoSecundario")),
                "price_total_bundle": clean_price(detail.get("totalFullUs")),
                "photo_count": len(fotos),
                "slab_ids": join_slabs(chapas, "Id"),
                "slab_numbers": join_slabs(chapas, "Numero"),
                "slab_widths_m": join_slabs(chapas, "Largura"),
                "slab_heights_m": join_slabs(chapas, "Altura"),
                "slab_lengths_in": join_slabs(chapas, "Length"),
                "slab_heights_in": join_slabs(chapas, "Height"),
                "slab_areas_sqmt": join_slabs(chapas, "TotalSqmt"),
                "slab_areas_sqft": join_slabs(chapas, "TotalSqft"),
                "slab_count_actual": len(chapas),
            })
            # Detail often carries a richer composition / populated country /
            # location the listing lacked; override only when non-empty.
            for src_key, dst_key in [("nomeComposicao", "composition"),
                                     ("siglaPais", "country_code"),
                                     ("localizacao", "location")]:
                v = detail.get(src_key)
                if v:
                    row[dst_key] = v

        # image_url_full / _thumb mirror the original's first-photo references.
        if photo_urls:
            row["image_url_full"] = photo_urls[0]
            thumb = self._thumb_from_full(photo_urls[0])
            row["image_url_thumb"] = thumb

        row["image_urls"] = photo_urls  # base joins these into image_urls
        return row

    @staticmethod
    def _thumb_from_full(full_url: str) -> str:
        """Convert /.../{stem}{ext} to /.../{stem}_crop_555x300{ext}."""
        if not full_url or "_crop_555x300" in full_url:
            return ""
        if "." in full_url.rsplit("/", 1)[-1]:
            stem, _dot, ext = full_url.rpartition(".")
            return f"{stem}_crop_555x300.{ext}"
        return ""


if __name__ == "__main__":
    BrumagranScraper().run()
