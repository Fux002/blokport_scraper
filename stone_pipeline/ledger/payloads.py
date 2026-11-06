"""The served payloads of the two leasing lanes, and the payload_hash over them (SYNC_LEDGER_DESIGN.md section 4, M3).

One function builds each payload from a row: a stored ledger row at serve time, or the record populate is about to
write (both carry the table's column names). The payload_hash is taken over that same dict, so it covers exactly
what is served: a served field that changes re-serves the entity, a field that is not served never does.

Beside the payload, the hash takes the versions of the content behind a served url that stays the same when its
bytes change, which the payload alone cannot show: a variety texture's fingerprint (variation.image_sha256, the
{Key}.png is re-made in place) and the ETags of a product's standardised files (CanonicalRow.standard_version), plus
the product contract below. No em dashes (design principle 2).
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from stone_pipeline.config.settings import CATEGORIES
from stone_pipeline.io import imagestore
from stone_pipeline.ledger.db import payload_hash

# a variation always belongs to a category (strict): branch is NOT NULL and constrained, so every served
# variation maps to one canonical category label. Derived from the pack-built category registry.
_CATEGORY = {c.name: c.label for c in CATEGORIES}

# Bump when the PRODUCT payload must re-serve to every product while each one's served dict is unchanged (e.g.
# Blokport starts reading a field it ignored before): it is in the product hash, so a bump flips every product
# 'dirty' exactly once. A new or changed served field needs no bump, the hash covers the dict.
PRODUCT_PAYLOAD_CONTRACT = "v6-model-3d"

# Served on the product payload, but Blokport reads them at create only (the stock lane owns visibility and the
# made-to-order state afterwards), so they are outside the hash: a change of one never re-serves a product.
_CREATE_ONLY = ("photo_ready", "made_to_order")


def payload_hash_of(payload: Mapping[str, Any], *versions: Any) -> str:
    """The payload_hash of a served payload plus the content versions behind its urls."""
    return payload_hash([json.dumps(payload, sort_keys=True, ensure_ascii=False), *versions])


def variation_payload(v: Mapping[str, Any]) -> dict:
    """The variation payload Blokport receives (GET /sync/v1/variations)."""
    return {
        "category": _CATEGORY.get(v["branch"], ""),
        "type": v["type"],
        "name": v["name"],
        "aliases": json.loads(v["aliases"] or "[]"),
        "image_url": v["image_url"] or "",
    }


def variation_hash(v: Mapping[str, Any]) -> str:
    """The variation's payload_hash: its served payload and its texture's fingerprint (a texture re-made at the same
    url re-serves)."""
    return payload_hash_of(variation_payload(v), v["image_sha256"])


def product_payload(p: Mapping[str, Any]) -> dict:
    """The product payload Blokport receives (GET /sync/v1/products). Everything in it is an external reference
    Blokport resolves on its side, except the supplier's port ids and the per-source company_id."""
    return {
        # category + type are OWNED by the variation (identity = category/type/name); the product INHERITS them via
        # variation_external_id. The NAMES are sent too, denormalized, for display/filtering only, so Medusa never
        # joins product -> variation to show the stone type. They equal the variation's by construction.
        "variation_external_id": p["variation_key"],
        "category": p["category"] or "", "type": p["type"] or "",
        "color": p["color"], "finish": p["finish"], "quality": p["quality"],
        "vendor": p["vendor"],   # agnostic company name; Medusa resolves it (kept for display + fallback)
        # company_id: the per-source Medusa company id set in :4200 (empty until pasted). Medusa allocates by this id
        # directly when present, else resolves by `vendor`. ENV-SPECIFIC: a dev company id differs from prod.
        "company_id": p["company_id"] or "",
        "title": p["title"], "description": p["description"], "handle": p["handle"],
        "weight": p["weight"], "length": p["length"],
        "width": p["width"], "height": p["height"],
        "origin_country_code": p["origin_country_code"],   # the stone's quarry country (origin display)
        # the SUPPLIER's shipping ports = the port of origin. Already Medusa port ids, so Medusa links them directly
        # instead of deriving ports from the quarry country. Empty means "no port", never "re-derive".
        "port_ids": json.loads(p["ports"] or "[]"),
        # supplier COLLECTION location: where the product is physically collected before shipping. Independent of
        # origin_country_code (provenance) and of ports (departure); null when the source configured none.
        "collection_country_code": p["collection_country_code"],
        "collection_city": p["collection_city"],
        "bundle_size": p["bundle_size"],   # under coordination: pallet model is retiring the multiplier
        # LOTS: every piece of a lot carries the same lot_code and its place in the cut order; Blokport groups the
        # catalog on lot_code. Both null outside a lot.
        "lot_code": p["lot_code"],
        "lot_position": p["lot_position"],
        # Images, the SAME set the working CSV import carried: thumbnail = the main display image, oriented_images =
        # a block's Front/Right/Back/Left shots, image_urls = the gallery. All are ingestion sources Medusa copies
        # into its own storage. The variety's texture ({Key}.png) rides on the VARIATION payload.
        "thumbnail": p["thumbnail_key"] or "",
        "oriented_images": json.loads(p["oriented_image_keys"] or "[]"),
        "image_urls": json.loads(p["product_image_keys"] or "[]"),
        # The 3D slab (the "Slab 3D on Blokport" contract): the lead slab's cut-out and its outline, ingestion sources
        # like the images; null when the product has none (a close-up, a block or tile).
        "model_3d": ({"cutout": p["model_3d_key"], "meta": imagestore.cutout_meta_url(p["model_3d_key"])}
                     if p["model_3d_key"] else None),
        # The no-photo rule's decision (populate._photo_ready), the same one that sends the stock out with reason
        # 'no_photo'. Read at create only: a product that is not ready is created as a hidden draft (_CREATE_ONLY).
        "photo_ready": bool(p["photo_ready"]),
        # Made to order (a free-size shape): on sale with no count. Read at create like photo_ready (_CREATE_ONLY).
        "made_to_order": bool(p["made_to_order"]),
    }


def product_hash(p: Mapping[str, Any], standard_version: Any) -> str:
    """The product's payload_hash: its served payload without the create-only fields, the contract, and the version
    of its standardised files (re-made in place under the same names)."""
    served = {k: v for k, v in product_payload(p).items() if k not in _CREATE_ONLY}
    return payload_hash_of(served, PRODUCT_PAYLOAD_CONTRACT, standard_version)
