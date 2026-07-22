"""Varsha Stones scraper, on ScraperBase via the public SlabWare API.

Varsha runs the same SlabWare WebMethod backend as Ferraz/Bruma/Polonine: a
paginated listing POST (/FullInventory.aspx/ObterListaBundles, `inicio` offset,
40 per page) plus a per-bundle detail POST that enriches each row with colour,
classification, per-slab arrays, photos and pricing. Varsha uses the upgraded
detail endpoint DetalheBundleNovo.

Varsha sells slab inventory (bundles of slabs), so the format is the constant
`slab`. The site sits behind Cloudflare, so use_curl_cffi routes HTTP through a
Chrome TLS fingerprint. The viewer is PUBLIC (no login/cookies needed) once the
TLS fingerprint passes; a warm-up GET seeds the session.

WATERMARKS (IMPORTANT):
    varshastones.slabware.com burns a visible watermark into its product photos.
    The image URLs captured here (image_url_full / photo_urls) are the
    *watermarked source URLs*. Fine for cataloguing/matching, but NOT customer
    -facing as-is: a later step must download, de-watermark, and re-host the
    clean images. We still capture the source URLs so that step has something to
    fetch.

Run:  python scrapers/varsha.py
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

try:  # allow both `python scrapers/varsha.py` and `python -m scrapers.varsha`
    from scrapers.base import ScraperBase
    from scrapers import slabware
    from scrapers.slabware import slab_get as _get, clean_price, join_slabs, parse_display_status
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase
    import slabware                                            # noqa: F401 (used by the wrappers below)
    from slabware import slab_get as _get, clean_price, join_slabs, parse_display_status

BASE = "https://varshastones.slabware.com"
PAGE_URL = f"{BASE}/FullInventory.aspx"            # public viewer, no ?S= token needed
API_URL = f"{BASE}/FullInventory.aspx/ObterListaBundles"
DETAIL_API_URL = f"{BASE}/FullInventory.aspx/DetalheBundleNovo"   # varsha's upgraded detail endpoint

PAGE_SIZE = 40  # server-controlled, confirmed via probe

API_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=utf-8",
    "origin": BASE,
    "referer": PAGE_URL,
    "x-requested-with": "XMLHttpRequest",
}

# SlabWare response parsing + photo/price/status helpers are shared across tenants (slabware.py,
# imported in the dual-mode bootstrap above); only `base` differs, bound here. Local names kept so the
# parse_product callsites are unchanged.
def join_photos(fotos):
    return slabware.join_photos(fotos, BASE)


def build_image_urls(bundle_id, filename):
    return slabware.build_image_urls(bundle_id, filename, BASE)


class VarshaScraper(ScraperBase):
    source = "varsha"
    category = "slab"        # Varsha is slab inventory (bundles of slabs); item 3 explicit format
    id_field = "bundle_id"   # images named varsha_<bundle_id>_<idx>
    use_curl_cffi = True     # Cloudflare-fronted SlabWare tenant
    # Route through the residential proxy, like polonine (same SlabWare/Cloudflare stack). The datacenter
    # egress was accepted intermittently but Cloudflare blocks it unpredictably -- a cold start died when
    # the warm-up GET was blocked (fatal). A residential IP passes reliably (polonine has been rock-solid on
    # it), so this removes the coin-flip. Declared as a capability (config/proxies.yaml maps it to the proxy
    # + secret); the JSON API is light on bandwidth and images bypass the proxy, so the SOAX cost is modest.
    proxy_capability = "cloudflare_residential"
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
    VarshaScraper().run()
