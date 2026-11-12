"""Test helpers for the products lane's icon gate (phase 1, sync._ELIGIBLE_PRODUCT): a product is served once its
variety has an icon in the product's own category. `add_icon` gives a ledger variety an icon in a category (the row
populate writes from emit's listing); `seed_slab_category` seeds the Slabs category attribute, so a canonical row with
`category_pcat_id=SLAB_PCAT` is stored under the `slabs` handle."""
from __future__ import annotations

from stone_pipeline.config.categories import category_by_handle
from stone_pipeline.io.variety_icons import icon_key
from stone_pipeline.ledger.db import now_iso

SLAB_PCAT = "pcat_slab_test"


def add_icon(ledger, key: str, handle: str = "slabs", etag: str = "etag") -> None:
    ledger.execute("INSERT OR REPLACE INTO variation_category_image (key, category_handle, s3_key, etag) "
                   "VALUES (?, ?, ?, ?)", (key, handle, icon_key(key, category_by_handle(handle).name), etag))


def seed_slab_category(ledger) -> None:
    now = now_iso()
    ledger.upsert("attribute", {"category": "category", "value": "Slabs", "medusa_id": SLAB_PCAT, "state": "synced",
                                "created_at": now, "updated_at": now}, pk=("category", "value"))
