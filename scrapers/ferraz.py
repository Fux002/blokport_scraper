"""Ferraz Brasil Inventory scraper, migrated onto ScraperBase.

Ferraz runs a SlabWare WebMethod backend: a paginated listing POST
(/FullInventory.aspx/ObterListaBundles, `inicio` offset, 40 per page) plus a
per-bundle detail POST (/FullInventory.aspx/DetalheBundle) that enriches each
row with classification, colour, per-slab arrays, multi-photo lists and pricing.
Site-specific bits here are those two POSTs and the bundle/detail flattening;
folders, image download + naming, CSV, logging, retries and the format column
are inherited.

Ferraz sells slab inventory (bundles of slabs cut from a block), so the format
is the constant `slab`.

QUIRK: the whole site sits behind Cloudflare's bot challenge. The original
monolith used curl_cffi to impersonate a Chrome TLS fingerprint; ScraperBase
uses httpx, which Cloudflare may block. A live run is needed to confirm whether
httpx gets through (it may require restoring curl_cffi impersonation). The
listing warm-up GET is reproduced to seed session cookies.

Run:  python scrapers/ferraz.py
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/ferraz.py` and `python -m scrapers.ferraz`
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

# The S= token from the URL. SlabWare accepts an empty json filter regardless of
# the token, but a fresh one is used to be safe.
S_TOKEN = ("9t5AptkpH/x2GD3oStI0wQzByO78puNBq5ZOBU2x8a1DsWdZJ9W+UFgwpAvr7qTXgoGcUrL"
           "nmVFKc1f53xV19kVT86meSR82TYzwo9Agi4tIXRQahJ4warnLVR8jPRR5Nlzhv/4jv1JmwSC9p"
           "dc1APdC6JsDPn80c4HIItTOWsDTMGQ/m4KWvF/vuGG1xQjPN2naAtXme+bu5+uO0hBOcWWESF+"
           "wB2pQt8LvkO+h5JyX1H2r3rTAczO9+1aOnEINoQybgacnN/c/T1fFwsW+EWUS8XgpOiJkM6UPR"
           "5tsTEeNWPUQ1RNMPKHbkoFCDNu0cyNLSh2evfTLqOzz2DeOB1jvLXtdEm5CGTjLQbSazi5YDZ3"
           "J/iI+wjOvyyKDLqsjQpxThT9Aqnouk+4m5j0bMQ0xdINjDRTu5eY3xwn8REhAbLryAPUB0NGE9"
           "hlAATDyNv72E4Y62lg+nOBU9naNm8Ga5dwl2DeNXGmFgFhCQjXfbWq8IbhX8Yw0H6IVORAP")

BASE = "https://www.ferrazbrasilinventory.com"
PAGE_URL = f"{BASE}/FullInventory.aspx?S={S_TOKEN}"
API_URL = f"{BASE}/FullInventory.aspx/ObterListaBundles"
DETAIL_API_URL = f"{BASE}/FullInventory.aspx/DetalheBundle"

PAGE_SIZE = 40  # server-controlled, confirmed via probe

# Confirmed image path pattern: <id>/<guid>.jpg is full-res, <id>/<guid>_crop_555x300.jpg the thumb.
IMAGE_PATH_FULL = "/backendGranite/cadastros/Bundles/fotos/{bundle_id}/{filename}"
IMAGE_PATH_THUMB = "/backendGranite/cadastros/Bundles/fotos/{bundle_id}/{stem}_crop_555x300{ext}"

API_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=utf-8",
    "origin": BASE,
    "referer": PAGE_URL,
    "x-requested-with": "XMLHttpRequest",
}

DISPLAY_RE = re.compile(r"class='([^']+)'", re.IGNORECASE)
# When unauthenticated, prices come back as '<a>Login for Price</a>' stubs.
_LOGIN_STUB_RE = re.compile(r'<\s*a\s|login\s*for\s*price', re.IGNORECASE)


def _get(d, key, default=''):
    v = d.get(key)
    if v is None or v == '':
        return default
    return v


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
    """Convert an array of photo paths to a list of full source URLs."""
    out = []
    for path in fotos or []:
        if not path:
            continue
        if path.startswith('http'):
            out.append(path)
        elif path.startswith('/'):
            out.append(BASE + path)
        else:
            out.append(BASE + '/' + path)
    return out


def build_image_urls(bundle_id, filename):
    """Return (full_url, thumb_url) for a bundle's primary photo."""
    if not bundle_id or not filename:
        return ('', '')
    if '.' in filename:
        stem, _dot, ext = filename.rpartition('.')
        ext = '.' + ext
    else:
        stem, ext = filename, '.jpg'
    full = BASE + IMAGE_PATH_FULL.format(bundle_id=bundle_id, filename=filename)
    thumb = BASE + IMAGE_PATH_THUMB.format(bundle_id=bundle_id, stem=stem, ext=ext)
    return (full, thumb)


class FerrazScraper(ScraperBase):
    source = "ferraz"
    category = "slab"        # Ferraz is slab inventory (bundles of slabs); item 3 explicit format
    id_field = "bundle_id"   # images named ferraz_<bundle_id>_<idx>
    use_curl_cffi = True   # Cloudflare-fronted SlabWare tenant
    page_delay = 4.0

    columns = [
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
    ]

    # --- listing ------------------------------------------------------------
    def _warm_up(self) -> None:
        """GET the page once to seed cookies and pass any Cloudflare check."""
        self.log.info("warming up session: GET %s...", PAGE_URL[:80])
        self.get(PAGE_URL)

    def _fetch_page(self, inicio: int) -> list:
        payload = {"inicio": inicio, "json": ""}
        r = self.post(API_URL, headers=API_HEADERS, content=json.dumps(payload))
        d = (r.json().get('d') or {})
        return json.loads(d.get('Bundles') or '[]')

    def _fetch_detail(self, bundle_id) -> dict:
        payload = {"IdBundle": bundle_id, "IdCampanha": 0}
        try:
            r = self.post(DETAIL_API_URL, headers=API_HEADERS, content=json.dumps(payload))
        except Exception as exc:
            self.record_failure('detail', bundle_id=bundle_id, error=str(exc))
            return {}
        d = (r.json().get('d') or {})
        return d.get('Bundle') or {}

    def list_products(self) -> Iterable[Any]:
        self._warm_up()
        first = self._fetch_page(0)
        if not first:
            self.log.error("first batch empty; aborting")
            return
        yield from first
        inicio = PAGE_SIZE
        while True:
            self.log.info("batch inicio=%d", inicio)
            batch = self._fetch_page(inicio)
            if not batch:
                self.log.info("empty batch at inicio=%d; done", inicio)
                break
            yield from batch
            inicio += PAGE_SIZE

    # --- parsing ------------------------------------------------------------
    def parse_product(self, item: dict) -> Optional[dict]:
        bundle_id = item.get('id')
        filename = item.get('fotoPrincipal') or ''
        full_url, thumb_url = build_image_urls(bundle_id, filename)

        row = {
            # From listing
            'bundle_id': _get(item, 'id'),
            'material_name': _get(item, 'nomeMaterial'),
            'composition': _get(item, 'nomeComposicao'),
            'block': _get(item, 'bloco'),
            'bundle_ref': _get(item, 'cavalete'),
            'finish': _get(item, 'acabamento'),
            'slab_count': _get(item, 'qtdChapas'),
            'slabs': _get(item, 'chapas'),
            'average_size': _get(item, 'averageSize'),
            'quality': _get(item, 'nomeQualidade'),
            'thickness': _get(item, 'nomeEspessura'),
            'price_1': _get(item, 'preco1'),
            'price_old_1': _get(item, 'precoAntigo1'),
            'price_2': _get(item, 'preco2'),
            'price_old_2': _get(item, 'precoAntigo2'),
            'display_status': parse_display_status(item.get('displayProduct', '')),
            'country_code': _get(item, 'siglaPais'),
            'country': _get(item, 'nomePais'),
            'image_filename_remote': filename,
            'image_url_full': full_url,
            'image_url_thumb': thumb_url,
            # Filled in by the detail call below
            'classification': '', 'color': '', 'location': '', 'currency': '',
            'description': '', 'video_url': '', 'slabs_available': '',
            'total_area_summary': '', 'avg_size_metric': '', 'avg_size_imperial': '',
            'total_sqft': '', 'total_sqmt': '', 'kitchen_visualizer_url': '',
            'price_main': '', 'price_main_old': '', 'price_secondary': '',
            'price_secondary_old': '', 'price_total_bundle': '',
            'photo_urls': '', 'photo_count': 0,
            'slab_ids': '', 'slab_numbers': '', 'slab_widths_m': '', 'slab_heights_m': '',
            'slab_lengths_in': '', 'slab_heights_in': '', 'slab_areas_sqmt': '',
            'slab_areas_sqft': '', 'slab_count_actual': '',
        }

        # Enrich with the DetalheBundle response.
        detail = self._fetch_detail(bundle_id)
        photo_url_list = self._merge_detail(row, detail)

        # image_urls for the base: full source photo URLs (from fotos[]), falling
        # back to the listing's primary full-res photo.
        if photo_url_list:
            image_urls = photo_url_list
        elif full_url:
            image_urls = [full_url]
        else:
            image_urls = []

        row['image_urls'] = image_urls
        return row

    def _merge_detail(self, row: dict, detail: dict) -> list:
        """Enrich row with DetalheBundle fields. Returns the list of full photo URLs."""
        if not detail:
            return []

        fotos = detail.get('fotos') or []
        chapas = detail.get('chapas') or []
        photo_url_list = join_photos(fotos)

        link = detail.get('linkProduto', '') or ''
        row.update({
            'classification': _get(detail, 'classificacao'),
            'color': _get(detail, 'cor'),
            'location': _get(detail, 'localizacao'),
            'currency': _get(detail, 'moeda'),
            'description': _get(detail, 'observacao'),
            'video_url': _get(detail, 'video'),
            'slabs_available': _get(detail, 'slabsAvailable'),
            'total_area_summary': _get(detail, 'totalValoresMaterialSlabs'),
            'avg_size_metric': _get(detail, 'averageSize'),
            'avg_size_imperial': _get(detail, 'averageSizeSecundario'),
            'total_sqft': _get(detail, 'totalSqft'),
            'total_sqmt': _get(detail, 'totalSqmt'),
            'kitchen_visualizer_url': BASE + link if link.startswith('/') else _get(detail, 'linkProduto'),
            'price_main': clean_price(detail.get('precoPrincipal')),
            'price_main_old': clean_price(detail.get('precoAntigoPrincipal')),
            'price_secondary': clean_price(detail.get('precoSecundario')),
            'price_secondary_old': clean_price(detail.get('precoAntigoSecundario')),
            'price_total_bundle': clean_price(detail.get('totalFullUs')),
            'photo_urls': ' | '.join(photo_url_list),
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

        # The detail response often has a richer composition string and a
        # populated country code/location the listing lacked. Override only if non-empty.
        for src_key, dst_key in [('nomeComposicao', 'composition'),
                                 ('siglaPais', 'country_code'),
                                 ('localizacao', 'location')]:
            v = detail.get(src_key)
            if v:
                row[dst_key] = v

        return photo_url_list


if __name__ == "__main__":
    FerrazScraper().run()
