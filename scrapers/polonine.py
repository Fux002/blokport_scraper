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

import json
import re
from typing import Any, Iterable, Optional

try:
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

BASE = "https://polonine.slabware.com"
S_TOKEN = (
    "9t5AptkpH/x2GD3oStI0wQzByO78puNBq5ZOBU2x8a1DsWdZJ9W+UFgwpAvr7qTXgoGcUrLnmVFKc1f53xV19kVT8"
    "6meSR82TYzwo9Agi4tIXRQahJ4warnLVR8jPRR5Nlzhv/4jv1JmwSC9pdc1APdC6JsDPn80c4HIItTOWsDTMGQ/m4K"
    "WvF/vuGG1xQjPN2naAtXme+bu5+uO0hBOcWWESF+wB2pQt8LvkO+h5JyX1H2r3rTAczO9+1aOnEINoQybgacnN/c/T"
    "1fFwsW+EWUS8XgpOiJkM6UPR5tsTEeNWPUQ1RNMPKHbkoFCDNu0cyNLSh2evfTLqOzz2DeOB1jvLXtdEm5CGTjLQbS"
    "azi5YDZ3J/iI+wjOvyyKDLqsjQpxThT9Aqnouk+4m5j0bMQ0xdINjDRTu5eY3xwn8REhAbLryAPUB0NGE9hlAATDyN"
    "v72E4Y62lg+nOBU9naNm8Ga5dwl2DeNXGmFgFhCQjXfbWq8IbhX8Yw0H6IVORAP"
)
PAGE_URL = f"{BASE}/FullInventory.aspx?S={S_TOKEN}"
LIST_API = f"{BASE}/FullInventory.aspx/ObterListaBundles"
DETAIL_API = f"{BASE}/FullInventory.aspx/DetalheBundle"
PAGE_SIZE = 40
API_HEADERS = {"content-type": "application/json; charset=UTF-8",
               "x-requested-with": "XMLHttpRequest", "origin": BASE, "referer": PAGE_URL}


def _g(d, key, default=""):
    v = (d or {}).get(key)
    return v if v not in (None, "") else default


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
    category = "slab"        # SlabWare slab inventory; explicit format (item 3)
    id_field = "product_id"
    use_curl_cffi = True     # Cloudflare-fronted
    needs_proxy = True       # tested: this tenant blocks the datacenter IP even with the Chrome fingerprint
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

    def _fetch_detail(self, bundle_id) -> dict:
        try:
            r = self.post(DETAIL_API, headers=API_HEADERS,
                          content=json.dumps({"IdBundle": bundle_id, "IdCampanha": 0}))
        except Exception as exc:
            self.record_failure("detail", bundle_id=bundle_id, error=str(exc))
            return {}
        return (r.json().get("d") or {}).get("Bundle") or {}

    def list_products(self) -> Iterable[Any]:
        self._warm_up()
        inicio = 0
        while True:
            batch = self._fetch_page(inicio)
            if not batch:
                break
            self.log.info("batch inicio=%d: %d bundles", inicio, len(batch))
            yield from batch
            inicio += PAGE_SIZE

    def parse_product(self, item: dict) -> Optional[dict]:
        detail = self._fetch_detail(item.get("id")) or {}
        chapas = detail.get("chapas") or item.get("chapas") or []
        first = chapas[0] if chapas else {}
        # slabs_detail in the polonine shape (the bundle resolver counts "n")
        slabs_detail = json.dumps([{
            "n": _g(c, "Numero"), "h_cm": _g(c, "Altura"), "w_cm": _g(c, "Largura"),
            "h_in": _g(c, "Height"), "w_in": _g(c, "Length"),
            "sqft": _g(c, "TotalSqft"), "sqmt": _g(c, "TotalSqmt"),
        } for c in chapas], ensure_ascii=False)
        return {
            "product_id": _g(item, "id"),
            "material": _g(item, "nomeMaterial"),
            "stone_type": _g(item, "nomeComposicao"),
            "color": _g(detail, "cor") or _g(item, "cor"),
            "finish": _g(item, "acabamento"),
            "quality": _g(item, "nomeQualidade"),
            "classification": _g(detail, "classificacao"),
            "thickness": _g(item, "nomeEspessura"),
            "block": _g(item, "bloco"),
            "bundle_no": _g(item, "cavalete"),
            "slab_numbers": _join(chapas, "Numero"),
            # qtdChapas is often null in the feed; the chapas array is the real count
            "slab_count": _g(item, "qtdChapas") or _g(detail, "qtdChapas") or (str(len(chapas)) if chapas else ""),
            "dimension_height": _g(first, "Altura"),
            "dimension_length": _g(first, "Largura"),
            "height_in": _g(first, "Height"),
            "length_in": _g(first, "Length"),
            "average_size": _g(item, "averageSize"),
            "area_sqft": _g(detail, "totalSqft"),
            "area_sqmt": _g(detail, "totalSqmt"),
            "weight": "",
            "origin": _g(detail, "nomePais") or _g(item, "nomePais"),
            "country_code": _g(detail, "siglaPais") or _g(item, "siglaPais"),
            "location": _g(detail, "localizacao"),
            "price1": _g(item, "preco1"),
            "price2": _g(item, "preco2"),
            "currency": _g(detail, "moeda") or _g(item, "moeda"),
            "status_flag": _status(item.get("displayProduct", "")),
            "detail_url": f"{BASE}/Product-Details.aspx?ID={_g(item, 'id')}",
            "slabs_detail": slabs_detail,
            "image_urls": _photos(detail.get("fotos")),  # base keeps these as source links
        }


if __name__ == "__main__":
    PolonineScraper().run()
