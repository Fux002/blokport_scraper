"""Zucchi scraper, migrated onto ScraperBase.

Zucchi runs a Salesforce-backed inventory (no public REST): products come from a
paginated Apex `execute` POST (buscaProds_WishlistWithTime). Site-specific bits
are the POST pagination and the nested-field flattening; folders, image download,
naming, CSV, and the format column are inherited.

Zucchi sells slab inventory, so the format is the constant `slab`.

Run:  python scrapers/zucchi.py
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

try:
    from scrapers.base import ScraperBase
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from base import ScraperBase

API_URL = "https://inventory.zucchistones.com/webruntime/api/apex/execute"
CHV_LOCATION = "AoVaGsGxUNGRCNi(AKb3#2vvuBn6EBCYBEpU3#C$"
STANDARD_LOCATION = "Padrão"
PAGE_SIZE = 12
NOTFOUND_MARKER = "notFoundGr"   # Salesforce placeholder image


def _get(d, *keys, default=""):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur if cur not in (None, "") else default


class ZucchiScraper(ScraperBase):
    source = "zucchi"
    category = "slab"      # Zucchi is slab inventory (item 3: explicit format)
    id_field = "bundle_id"  # images named zucchi_<bundle_id>_<idx>
    page_delay = 4.0
    columns = [
        "bundle_id", "product_name_en", "product_name_pt", "product_full_name",
        "family", "classification", "thickness", "finish", "block",
        "height_m", "height_in", "length_m", "length_in",
        "area_m2", "area_ft2", "weight_kg_net", "weight_kg_gross",
        "slab_count", "slab_sequence",
        "unit_price_usd_per_ft2", "total_price_usd", "currency",
        "water_absorption", "apparent_density", "compressive_strength", "flexural_strength",
        "description", "situation", "situation_real", "situation_commercial",
        "location_code", "days_in_stock", "date_available", "last_modified", "video_url",
    ]

    def _fetch_page(self, pagination: int, count: bool = False) -> dict:
        payload = {
            "namespace": "", "classname": "@udd/01p8X000004WUED",
            "method": "buscaProds_WishlistWithTime", "isContinuation": False,
            "params": {
                "chvLocation": CHV_LOCATION, "idContact": "", "pagination": pagination,
                "chvWishlist": None, "qtdRegs": PAGE_SIZE, "filters": None, "order": None,
                "searchTerm": None, "standardLocation": STANDARD_LOCATION, "contarProds": count,
            },
            "cacheable": False,
        }
        r = self.post(API_URL, params={"language": "en-US", "asGuest": "true", "htmlEncode": "false"},
                      content=json.dumps(payload), headers={"content-type": "application/json"})
        return r.json().get("returnValue", {}) or {}

    def list_products(self) -> Iterable[Any]:
        first = self._fetch_page(0, count=True)
        total = first.get("contagemProds", 0)
        items = first.get("itens", []) or []
        if not items or not total:
            return
        self.log.info("zucchi: %d products", total)
        yield from items
        fetched = len(items)
        # `pagination` is a PAGE INDEX (0,1,2,...), NOT a row offset -- page 0 is the first 12,
        # page 1 the next 12. Advance the page number, not the cumulative item count, or the scrape
        # samples every 12th page and quits early (~180 of 2036).
        page = 1
        while fetched < total:
            batch = self._fetch_page(page).get("itens", []) or []
            if not batch:
                # ran out of items before the advertised total -> the catalog was truncated (a
                # transient empty/blocked page, not a real end). Mark the run incomplete so the
                # pipeline won't treat this short scrape as authoritative and delist the missing.
                self.mark_incomplete(f"pagination stopped at {fetched}/{total} products")
                break
            yield from batch
            fetched += len(batch)
            page += 1

    def parse_product(self, item: dict) -> Optional[dict]:
        image_urls, video_url = [], ""
        for content in item.get("contents", []) or []:
            url = content.get("prodLinkFull") or ""
            if not url or NOTFOUND_MARKER in url:
                continue
            if content.get("isImage"):
                image_urls.append(url)
            elif content.get("isVideo") and not video_url:
                video_url = url
        si = ("prod", "Stock_Item__r")
        pr = (*si, "Produto__r")
        return {
            "bundle_id": _get(item, *si, "Codigo__c") or _get(item, "prod", "Name"),
            "product_name_en": _get(item, *pr, "NomeMaterialEn__c"),
            "product_name_pt": _get(item, *pr, "NomeMaterialPt__c"),
            "product_full_name": _get(item, *pr, "Name"),
            "family": _get(item, *pr, "FamilyVitrine__c") or _get(item, *si, "Familia__c"),
            "classification": _get(item, *si, "Classificacao__c"),
            "thickness": _get(item, *si, "EspessuraProd__c"),
            "finish": _get(item, *si, "Acabamento__c"),
            "block": _get(item, *si, "Bloco__c"),
            "height_m": _get(item, *si, "AlturaLiquidaM__c"),
            "height_in": _get(item, *si, "AlturaLiquidaIn__c"),
            "length_m": _get(item, *si, "ComprimentoLiquidoM__c"),
            "length_in": _get(item, *si, "ComprimentoLiquidoIn__c"),
            "area_m2": _get(item, *si, "MetragemLiquidaM2__c"),
            "area_ft2": _get(item, *si, "MetragemLiquidaFt2__c"),
            "weight_kg_net": _get(item, *si, "Peso__c"),
            "weight_kg_gross": _get(item, *si, "PesoBruto__c"),
            "slab_count": _get(item, *si, "QuantidadeChapas__c"),
            "slab_sequence": _get(item, *si, "SequenciaChapas__c"),
            "unit_price_usd_per_ft2": _get(item, "parsedData", "UnitPrice") or _get(item, *si, "PrecoFFt2__c"),
            "total_price_usd": _get(item, "parsedData", "TotalPrice") or _get(item, *si, "PrecoTotal__c"),
            "currency": _get(item, "parsedData", "CurrencyIsoCode") or _get(item, *si, "CurrencyIsoCode", default="USD"),
            "water_absorption": _get(item, *pr, "WatAbs__c"),
            "apparent_density": _get(item, *pr, "AppDde__c"),
            "compressive_strength": _get(item, *pr, "ComStr__c"),
            "flexural_strength": _get(item, *pr, "FleRes__c"),
            "description": _get(item, *pr, "ProDesIng__c"),
            "situation": _get(item, *si, "Situacao__c"),
            "situation_real": _get(item, *si, "SituacaoReal__c"),
            "situation_commercial": _get(item, *si, "SituacaoComercial__c"),
            "location_code": _get(item, *si, "Localizacao__c"),
            "days_in_stock": _get(item, *si, "Dias__c"),
            "date_available": _get(item, *si, "DataDisponibilizacao__c"),
            "last_modified": _get(item, *si, "LastModifiedDate"),
            "video_url": video_url,
            "image_urls": image_urls,   # base downloads + names them
        }


if __name__ == "__main__":
    ZucchiScraper().run()
