"""Polonine scraper, on ScraperBase via the SlabWare API (no browser).

Polonine runs the same SlabWare platform as Ferraz/Bruma/Varsha. The original
polonine scraper used Playwright because the AJAX API had not been reverse
-engineered yet; it has now (ObterListaBundles + DetalheBundle), so this scraper
hits the API directly, which is far simpler and faster than driving Chromium.

It maps the SlabWare bundle objects to the SAME column names the pipeline's
polonine adapter expects (product_id, material, stone_type, color, finish,
quality, thickness, slab_count, area_sqmt, slabs_detail, image_urls, ...), so the
output drops straight into the existing pipeline.

Polonine sells slab inventory (bundles of slabs) -> constant format `slab`.
Cloudflare fronts SlabWare, so use_curl_cffi routes HTTP through a Chrome TLS
fingerprint (install once: pip3 install curl_cffi).

Verified live against the tenant API.

Run:  python scrapers/polonine.py
"""

from __future__ import annotations

from urllib.parse import urlsplit
import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase
from stone_pipeline.core import env
from stone_pipeline.io.ssrf import SSRFBlocked

# The public FullInventory viewer needs no ?S= token (verified live: list + detail both 200).
BASE = "https://polonine.slabware.com"
PAGE_URL = f"{BASE}/FullInventory.aspx"
LIST_API = f"{BASE}/FullInventory.aspx/ObterListaBundles"
DETAIL_API = f"{BASE}/FullInventory.aspx/DetalheBundle"
PAGE_SIZE = 40
API_HEADERS = {"content-type": "application/json; charset=UTF-8",
               "x-requested-with": "XMLHttpRequest", "origin": BASE, "referer": PAGE_URL}
# The customer login that shows prices ("Prices ... are available for logged in customers only"). Both values come from
# the environment (SSM-backed when deployed), never from code or config: the username is a person's email. The endpoints are the site's own login and logout (slabware.js).
LOGIN_API = f"{BASE}/ajax/Ajax.aspx/Modal_Login"
LOGOUT_API = f"{BASE}/ajax/Ajax.aspx/LogOut_Head"
LOGIN_USER_ENV = "SCRAPER_POLONINE_USERNAME"
LOGIN_PASSWORD_ENV = "SCRAPER_POLONINE_PASSWORD"
LOGIN_OK = "ok"
# What a price-pass request can end in besides an answer: no answer after the retries (RuntimeError), an answer that is
# not JSON (ValueError, JSONDecodeError among them), a refused redirect (SSRFBlocked), JSON that is not an object
# (AttributeError on .get). Each reads no prices; none may reach the product scrape.
PRICE_PASS_FAILURES = (RuntimeError, ValueError, SSRFBlocked, AttributeError)
# The price pass's rows (prices.csv): the bundle's own list fields, the same values the product row takes from the list.
PRICE_COLUMNS = ["product_id", "material", "stone_type", "finish", "quality", "thickness", "block", "bundle_no",
                 "average_size", "price1", "price2", "detail_url"]


def _as_the_login_form_sends(value: str) -> str:
    """A login value as the site's own form sends it: slabware.js strips quotes from both fields."""
    return value.replace("'", "").replace('"', "")


def _g(d, key, default=""):
    v = (d or {}).get(key)
    return v if v not in (None, "") else default


def _list_fields(item: dict) -> dict:
    """The bundle's fields as the LIST states them: the product row's and the price row's one mapping."""
    return {
        "product_id": _g(item, "id"),
        "material": _g(item, "nomeMaterial"),
        "stone_type": _g(item, "nomeComposicao"),
        "finish": _g(item, "acabamento"),
        "quality": _g(item, "nomeQualidade"),
        "thickness": _g(item, "nomeEspessura"),
        "block": _g(item, "bloco"),
        "bundle_no": _g(item, "cavalete"),
        "average_size": _g(item, "averageSize"),
        "price1": _g(item, "preco1"),
        "price2": _g(item, "preco2"),
        "detail_url": f"{BASE}/Product-Details.aspx?ID={_g(item, 'id')}",
    }


def _join(chapas, key):
    return " | ".join(str((c or {}).get(key, "")) for c in (chapas or []))


def _photos(fotos):
    out = []
    for path in fotos or []:
        path = str(path or "").strip()
        if path:
            out.append(path if path.startswith("http") else BASE + ("" if path.startswith("/") else "/") + path)
    return out


def _status(html):
    m = re.search(r">([^<]+)<", str(html or ""))
    return m.group(1).strip() if m else ""


class PolonineScraper(ScraperBase):
    source = "polonine"
    site_domain = urlsplit(BASE).hostname   # the supplier's public site, from its own URL
    category = "slab"        # SlabWare slab inventory; explicit format (item 3)
    id_field = "product_id"
    use_curl_cffi = True     # Cloudflare-fronted
    # tested: this tenant blocks the datacenter IP even with the Chrome fingerprint, so it needs a
    # residential proxy. Declared as a capability (config/proxies.yaml maps it to the residential proxy +
    # its secret); the toolbox way, so swapping the provider is a config edit.
    proxy_capability = "cloudflare_residential"
    columns = [
        "product_id", "material", "stone_type", "color", "finish", "quality",
        "classification", "thickness", "block", "bundle_no", "slab_numbers", "slab_count",
        "dimension_height", "dimension_length", "height_in", "length_in",
        "average_size", "area_sqft", "area_sqmt", "weight",
        "origin", "country_code", "location", "price1", "price2", "currency",
        "status_flag", "detail_url", "slabs_detail",
    ]

    def _warm_up(self) -> None:
        self.log.info("warm up: GET %s...", PAGE_URL[:70])
        self.get(PAGE_URL)

    def _fetch_page(self, inicio: int) -> list:
        r = self.post(LIST_API, headers=API_HEADERS, content=json.dumps({"inicio": inicio, "json": ""}))
        d = r.json().get("d") or {}
        return json.loads(d.get("Bundles") or "[]")

    def _fetch_detail(self, bundle_id) -> Optional[dict]:
        """The bundle detail, or None when the fetch FAILED (recorded + counted for the delist-failure ratio):
        the caller then HOLDS the row for a retry instead of shipping it half-formed."""
        try:
            r = self.post(DETAIL_API, headers=API_HEADERS,
                          content=json.dumps({"IdBundle": bundle_id, "IdCampanha": 0}))
        except Exception as exc:
            self.record_failure("detail", bundle_id=bundle_id, error=str(exc))
            self.note_detail(ok=False)   # A1: feed the delist gate's detail-failure ratio
            return None
        self.note_detail(ok=True)
        return (r.json().get("d") or {}).get("Bundle") or {}

    def _login(self) -> str:
        """"ok" when the customer login that shows prices succeeded, else why not. Never raises: the product and
        stock scrape needs no login."""
        user, password = env.getenv(LOGIN_USER_ENV), env.getenv(LOGIN_PASSWORD_ENV)
        if not user or not password:
            return f"no login in the environment ({LOGIN_USER_ENV}, {LOGIN_PASSWORD_ENV})"
        body = json.dumps({"login": _as_the_login_form_sends(user), "senha": _as_the_login_form_sends(password)})
        try:
            answer = (self.post(LOGIN_API, headers=API_HEADERS, content=body).json() or {}).get("d")
        except PRICE_PASS_FAILURES as exc:
            return f"login request failed: {type(exc).__name__}"
        # a refusal is stated by its length, never the site's words: they could echo the username back
        return LOGIN_OK if answer == LOGIN_OK else f"login refused ({len(str(answer))} characters)"

    def _logout(self) -> None:
        """End the customer session. A logout that fails is logged, not fatal: the session expires."""
        try:
            self.post(LOGOUT_API, headers=API_HEADERS, content="{}")
        except PRICE_PASS_FAILURES as exc:
            self.log.warning("logout got no answer: %s", type(exc).__name__)

    def list_products(self) -> Iterable[Any]:
        # SlabWare exposes no total, so the base paginator confirms an empty batch with one re-probe before
        # accepting the end (a transient empty must not truncate the tail -> silent delist).
        self._warm_up()
        yield from self.paginate_offset(self._fetch_page, PAGE_SIZE)

    def run(self) -> Path:
        """The anonymous product and stock scrape exactly as before, then the separate logged-in price pass. The product
        set never depends on the login: the pass writes only its own files, after the products and their completion
        marker are written, and no failure of it reaches the products: price files that cannot be written are logged,
        and the products are returned all the same."""
        products = super().run()
        try:
            self._price_pass()
        except OSError as exc:
            self.log.error("price pass files not written: %s: %s", type(exc).__name__, exc)
        return products

    def _price_pass(self) -> None:
        """Log in, read the list again (it carries the prices to a logged-in customer), log out, and write the prices
        with the pass's statement ("ok" or why not). A missing, refused or failing login reads no prices and is logged
        loudly; nothing of the product scrape changes."""
        prices = self._login()
        rows: list[dict] = []
        if prices == LOGIN_OK:
            try:
                rows = [_list_fields(item) for item in self.paginate_offset(self._fetch_page, PAGE_SIZE)]
            except PRICE_PASS_FAILURES as exc:
                prices, rows = f"price list failed: {type(exc).__name__}", []
            finally:
                self._logout()
        if prices != LOGIN_OK:
            self.log.error("price pass read no prices: %s", prices)
        self.write_price_pass(rows, PRICE_COLUMNS, prices)

    def parse_product(self, item: dict) -> Optional[dict]:
        fetched = self._fetch_detail(item.get("id"))
        detail = fetched or {}
        chapas = detail.get("chapas") or item.get("chapas") or []
        first = chapas[0] if chapas else {}
        # slabs_detail in the polonine shape (the bundle resolver counts "n")
        slabs_detail = json.dumps([{
            "n": _g(c, "Numero"), "h_cm": _g(c, "Altura"), "w_cm": _g(c, "Largura"),
            "h_in": _g(c, "Height"), "w_in": _g(c, "Length"),
            "sqft": _g(c, "TotalSqft"), "sqmt": _g(c, "TotalSqmt"),
        } for c in chapas], ensure_ascii=False)
        row = {
            **_list_fields(item),
            "color": _g(detail, "cor") or _g(item, "cor"),
            "classification": _g(detail, "classificacao"),
            "slab_numbers": _join(chapas, "Numero"),
            # qtdChapas is often null in the feed; the chapas array is the real count
            "slab_count": _g(item, "qtdChapas") or _g(detail, "qtdChapas") or (str(len(chapas)) if chapas else ""),
            "dimension_height": _g(first, "Altura"),
            "dimension_length": _g(first, "Largura"),
            "height_in": _g(first, "Height"),
            "length_in": _g(first, "Length"),
            "area_sqft": _g(detail, "totalSqft"),
            "area_sqmt": _g(detail, "totalSqmt"),
            "weight": "",
            "origin": _g(detail, "nomePais") or _g(item, "nomePais"),
            "country_code": _g(detail, "siglaPais") or _g(item, "siglaPais"),
            "location": _g(detail, "localizacao"),
            "currency": _g(detail, "moeda") or _g(item, "moeda"),
            "status_flag": _status(item.get("displayProduct", "")),
            "slabs_detail": slabs_detail,
            "image_urls": _photos(detail.get("fotos")),  # base keeps these as source links
        }
        if fetched is None:
            # dims, colour, area and photos live only in the detail: HOLD the row for a retry (the pipeline
            # holds a fetch-failed dimension instead of defaulting it), exactly as varsha and marenostone do.
            self.mark_fetch_failed(row, "dims", bundle_id=item.get("id"), error="detail fetch failed")
        return row


if __name__ == "__main__":
    PolonineScraper().run()
