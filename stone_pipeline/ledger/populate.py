"""Populate the ledger from canonical rows (the write-through, design section 3).

The live pipeline's write-through (ledger.writethrough) calls it on every produce, so the ledger Medusa pulls holds
the run's products, stock and variations. The product row stores the design's name-based fields (ids resolved at
render) plus the presentation fields the legacy import CSV needs. No em dashes (design principle 2).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from stone_pipeline.aliases import payload as alias_payload
from stone_pipeline.config.categories import category
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.io import imagestore
from stone_pipeline.io.variety_icons import ICON_SIDECAR, icon_key
from stone_pipeline.ledger.db import CLEAR_STOCK_FAILURES_SQL, Ledger, in_medusa_sql, now_iso, variation_icons
from stone_pipeline.ledger.payloads import product_hash, variation_hash
from stone_pipeline.phase1.variety_key import variety_key_problem
from stone_pipeline.stages.emit import _sku
from stone_pipeline.stages.emit_catalog import _COLS as _VARIANTS_FULL_COLS
from stone_pipeline.stages.product_state import inventory_str

def read_icon_sidecar(path: Path) -> dict[str, list[dict]] | None:
    """{Key: [icon row]} from emit's side file, each row a `variation_category_image` row (category handle, the pair's
    S3 key, its ETag). None when there is no file: S3 was not listed (CI, local), so the stored icons stand and nothing
    is served from a guess. A malformed file, a category name the pack does not have or an empty ETag raises."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ValueError(f"{path.name}: expected {{Key: {{category name: ETag}}}}")
    out: dict[str, list[dict]] = {}
    for key, by_category in data.items():
        rows = []
        for name, etag in by_category.items():
            if not isinstance(etag, str) or not etag:
                raise ValueError(f"{path.name}: {key} {name} has no ETag")
            s3_key = icon_key(key, name)   # refuses a Key the parser refuses and a name that is no pack category's
            rows.append({"category_handle": category(name).handle, "s3_key": s3_key, "etag": etag})
        out[key] = rows
    return out


def populate_variations_full(ledger: Ledger, path: str | Path) -> int:
    """Reflect a produced variety file (1_variants_full.csv, or the base on a reset) onto the variation table: write
    each variety's content (type, name, alias_rows, codes) and its icons, and mark it in_full=1; insert a new variety
    as pending (no Medusa id yet). Rows not in the file are reset to in_full=0 so they never render. medusa_id, state
    and first_seen are preserved on existing rows; only the produced content moves, and a content change re-serves.
    alias_rows and codes come from the alias sidecar emit writes beside the file (aliases.payload); with no sidecar
    (the base a reset populates from) they are NULL, not computed, and held until a produce computes them. A sidecar
    without a produced variety fails loud: a NULL there would hold a variety the produce did compute."""
    _key, _name, _type, _ = _VARIANTS_FULL_COLS
    now = now_iso()
    sidecar = read_icon_sidecar(Path(path).parent / ICON_SIDECAR)
    alias_sidecar = alias_payload.read_sidecar(Path(path).parent)
    stored = variation_icons(ledger) if sidecar is None else {}
    ledger.execute("UPDATE variation SET in_full = 0")
    n = 0
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            key = (r.get(_key) or "").strip()
            if not key:
                continue
            if (problem := variety_key_problem(key)) is not None:
                raise ValueError(f"{Path(path).name}: {key!r} is not a variety Key ({problem})")
            name, type_ = r.get(_name) or "", r.get(_type) or ""
            alias_rows, codes = _alias_cells(alias_sidecar, key, name, Path(path).name)
            icons = sidecar.get(key, []) if sidecar is not None else stored.get(key, [])
            ph = variation_hash({"type": type_, "name": name, "alias_rows": alias_rows, "codes": codes}, icons)
            ledger.execute(
                "INSERT INTO variation (key, type, name, alias_rows, codes, medusa_id, in_full, payload_hash, "
                "state, first_seen, last_synced, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET type = excluded.type, name = excluded.name, "
                "alias_rows = excluded.alias_rows, codes = excluded.codes, in_full = 1, "
                "payload_hash = excluded.payload_hash, "
                # a content change re-serves (synced -> dirty); unchanged keeps its state. medusa_id
                # and first_seen are never touched, so an acked id survives a re-run. E5/un-retire: a
                # 'retiring' variety that reappears in the produced set (its exclusion was cleared) must
                # re-serve, not stay retiring -- flip it back to dirty.
                "state = CASE WHEN variation.state = 'retiring' THEN 'dirty' "
                "WHEN variation.payload_hash != excluded.payload_hash THEN 'dirty' "
                "ELSE variation.state END, "
                # updated_at is the syncing-lease clock (reap_stale_syncing): it moves with the row's
                # content or state, never on an unchanged re-run, or every produce would renew a lease
                # Medusa never acked and the row would stay in-flight instead of re-serving.
                "updated_at = CASE WHEN variation.state = 'retiring' "
                "OR variation.payload_hash != excluded.payload_hash THEN excluded.updated_at "
                "ELSE variation.updated_at END, "
                # new content is served at once, not after the wait a failed apply of the old content set, and is
                # no longer the dead-letter the operator abandoned
                "retry_after = CASE WHEN variation.payload_hash != excluded.payload_hash THEN NULL "
                "ELSE variation.retry_after END, "
                "abandoned_at = CASE WHEN variation.payload_hash != excluded.payload_hash THEN NULL "
                "ELSE variation.abandoned_at END",
                (key, type_, name, alias_rows, codes, None, ph, "pending", now, None, now, now),
            )
            if sidecar is not None:   # the listing is the icons' authority: a pair gone from S3 is gone here
                ledger.execute("DELETE FROM variation_category_image WHERE key = ?", (key,))
                ledger.executemany(
                    "INSERT INTO variation_category_image (key, category_handle, s3_key, etag) VALUES (?, ?, ?, ?)",
                    [(key, i["category_handle"], i["s3_key"], i["etag"]) for i in icons])
            n += 1
    # E5: a re-minted variety must not carry a stale variation-tombstone, or Medusa's /removed pull would
    # delete the freshly re-created variety. Clear any variation tombstone now back in the produced set.
    ledger.execute("DELETE FROM removed WHERE kind = 'variation' AND external_id IN "
                   "(SELECT key FROM variation WHERE in_full = 1)")
    return n


def _alias_cells(sidecar: dict | None, key: str, name: str, file: str) -> tuple[str | None, str | None]:
    """The stored (alias_rows, codes) JSON of one produced variety: NULL, NULL without a sidecar (not computed)."""
    if sidecar is None:
        return None, None
    if key not in sidecar:
        raise ValueError(f"{file}: {key} {name!r} is not in {alias_payload.ALIAS_SIDECAR}: emit computes every "
                         "produced variety's alias_rows and codes")
    entry = sidecar[key]
    return json.dumps(entry["alias_rows"], ensure_ascii=False), json.dumps(entry["codes"], ensure_ascii=False)


def _variation_key_for(ledger: Ledger, variation_id: str | None) -> str | None:
    """The product references its variation by Key; the canonical row carries the
    Medusa variation_id, so map it back through the seeded variation table."""
    if not variation_id:
        return None
    row = ledger.execute(
        "SELECT key FROM variation WHERE medusa_id = ? LIMIT 1", (variation_id,)
    ).fetchone()
    return row["key"] if row else None


def _category_handle_for(ledger: Ledger, pcat_id: str | None) -> str | None:
    """The product's category HANDLE (D11, re-seed safe): the row carries the pcat id, reverse-mapped through the
    seeded category attribute to its label, whose pack category gives the handle. A label no pack category has
    raises: a product is never stored under a guessed category."""
    if not pcat_id:
        return None
    row = ledger.execute(
        "SELECT value FROM attribute WHERE category = 'category' AND medusa_id = ? LIMIT 1",
        (pcat_id,),
    ).fetchone()
    if row is None:
        return None
    if (cat := category(row["value"])) is None:
        raise ValueError(f"category {row['value']!r} (pcat {pcat_id}) is no pack category")
    return cat.handle


def _photo_ready(r: CanonicalRow, prev, cfg: SourceConfig) -> bool:
    """The no-photo rule, decided here once per product: with images.require_standard_photo on, a product without a
    standardised photo is not ready. A run that cannot tell (standard_photo None: the photo pool unread or empty) keeps
    an existing product's last decision, and a product new to the ledger starts not ready (created as a hidden draft,
    shown once a run sees its photo). Without the rule every product is ready, and so is every product of a source whose
    image pipeline is off (cfg.image_pipeline): it shows its scraped photos, so it is never hidden, a new one never
    starts hidden, and one hidden while the pipeline was on is shown on the next full produce. populate_inventory hides
    exactly the products this marks not ready, so the product payload and the stock reason come from one decision."""
    if not (SETTINGS.images.require_standard_photo and cfg.image_pipeline):
        return True
    if r.standard_photo is None:
        return bool(prev["photo_ready"]) if prev else False
    return r.standard_photo


def populate_products(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> int:
    """Write canonical rows into the `product` table, in input order. New entities
    land `pending`; the equivalence test renders them straight back. Each product's photo fact (photo_ready) is
    decided here, before its stock (populate_inventory reads it)."""
    now = now_iso()
    n = 0
    for r in rows:
        sku = _sku(r, cfg)
        prev = ledger.get("product", "sku", sku)
        # Link the product to its variety by the STABLE Key. The canonical row now carries
        # variation_key, stamped at match time (match_variation._key_for) -- it never churns. Fall
        # back to the legacy id->Key lookup (via variation.medusa_id, which DOES churn: export id ->
        # minted id on ack) and then to the already-stored Key, so a row missing the stamp (older
        # data, bootstrap) still resolves and a re-populate never stomps a good link with NULL.
        variation_key = r.variation_key or _variation_key_for(ledger, r.variation_id) \
            or (prev["variation_key"] if prev else None)
        category = _category_handle_for(ledger, r.category_pcat_id)   # once: used in the record AND the hash
        photo_ready = _photo_ready(r, prev, cfg)
        record = {
            "sku": sku,
            "source": cfg.source_code,
            "vendor": cfg.vendor or cfg.source,   # the company this source belongs to
            "surrogate_key": r.surrogate_key,
            "variation_key": variation_key,
            "color": r.color_name,
            "finish": r.finish_name,
            "quality": r.quality_name,
            "type": r.type_name,
            "category": category,
            "title": r.title,
            "description": r.description,
            "handle": r.handle,
            "slug": r.slug,
            "weight": r.weight,
            "length": r.length,
            "width": r.width,
            "height": r.height,
            "origin_country_code": r.origin_country_code,
            "origin_city": r.origin_city,
            "origin_county": r.origin_county,
            "collection_country_code": r.collection_country_code,
            "collection_city": r.collection_city,
            "thumbnail_key": r.thumbnail_key,
            "oriented_image_keys": json.dumps(r.oriented_image_keys or []),
            "product_image_keys": json.dumps(r.product_image_keys or []),
            "model_3d_key": r.model_3d_key,
            "company_id": r.company_id,   # already = source_cfg.company_id or the default (constants.py)
            "sales_channel_id": r.sales_channel_id,
            "ports": json.dumps(r.port_ids or []),
            "visibility": r.visibility,
            "discountable": r.discountable,
            "sold_in_bundle": 1 if r.sold_in_bundle else 0,
            "bundle_size": r.bundle_size,
            "inventory_quantity": inventory_str(r),
            "made_to_order": 1 if r.made_to_order else 0,
            "lot_code": r.lot_code,
            "lot_position": r.lot_position,
            "photo_ready": 1 if photo_ready else 0,
            "created_at": now,
            "updated_at": now,
        }
        # the hash is the served payload (payloads.product_hash), so a served field that changes re-serves the product
        # and one that is not served (sales_channel_id: Blokport resolves the channel by vendor) never does
        record["payload_hash"] = ph = product_hash(record, r.standard_version)
        # Convergent sync state (design db.py header): NEW -> pending, CHANGED -> dirty, UNCHANGED
        # -> untouched. CRITICAL: preserve a synced product's medusa_id + state on re-run. The old
        # code hardcoded state='pending', medusa_id=None, so every write-through run reset the whole
        # catalog to pending and wiped the acked Medusa ids -> Medusa re-ingested everything nightly.
        # updated_at is the syncing-lease clock (reap_stale_syncing): it moves only with the content, so
        # an unchanged re-run never renews a lease Medusa did not ack. The row is still written (fields that are not
        # served may move), only the timestamp is carried over.
        if prev is None:                                     # `prev` fetched above (variation_key fallback)
            record.update(medusa_id=None, state="pending", last_synced=None)
        elif prev["payload_hash"] != ph:   # new content is served at once, not after a failure's wait, nor abandoned
            record.update(medusa_id=prev["medusa_id"], last_synced=prev["last_synced"], state="dirty", retry_after=None,
                          abandoned_at=None)
        else:
            record.update(medusa_id=prev["medusa_id"], last_synced=prev["last_synced"],
                          state=prev["state"], updated_at=prev["updated_at"])
        ledger.upsert("product", record, pk=("sku",), keep_on_update=("created_at", "first_seen"))
        n += 1
    # A re-created SKU must not carry a stale tombstone: a re-added vendor reuses deterministic SKUs, so
    # clear any PRODUCT tombstone whose product now exists, or Medusa's /removed pull would delete a live product.
    ledger.execute("DELETE FROM removed WHERE kind = 'product' AND external_id IN (SELECT sku FROM product)")
    return n


def ledger_products(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> list[CanonicalRow]:
    """The rows whose product the ledger already holds, in input order: the ones a stock-only run may move
    the stock of (the inventory row references the product)."""
    held = {r["sku"] for r in ledger.execute("SELECT sku FROM product")}
    return [r for r in rows if _sku(r, cfg) in held]


def _stock_of(row) -> tuple:
    """The stock the inventory lane serves (db.STOCK_DELTA_SQL compares the same four columns)."""
    return row["qty"], row["made_to_order"], row["reason"], row["replaced_by_lot_code"]


def _write_stock(ledger: Ledger, stored, record: dict) -> None:
    """Write one stock row. The last synced stock is not in `record`, so an update keeps it: it is owned by the ack,
    and overwriting it each run would re-serve every product as a delta forever (a new row starts never synced). A
    new stock clears the failure wait of the old one (CLEAR_STOCK_FAILURES_SQL): it is served at once, as new product
    content is."""
    ledger.upsert("inventory", record, pk=("sku",))
    if stored is not None and _stock_of(stored) != _stock_of(record):
        ledger.execute(f"UPDATE inventory SET {CLEAR_STOCK_FAILURES_SQL} WHERE sku = ?", (record["sku"],))


def populate_inventory(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> int:
    """Write per-product stock into the `inventory` table (qty = the derived inventory_quantity field, NULL for made
    to order). The last synced stock is left null, so a freshly populated row is a delta to serve (design section
    7). Products must be populated first (the sku FK, and the photo fact). A product the no-photo rule marked not
    ready (product.photo_ready 0, decided by populate_products) is hidden: stock 0, reason 'no_photo' (always with
    stock 0, never made to order: Blokport hides on stock 0 and reads an unknown reason above 0 as a plain quantity;
    'delisted' stays the vendor take-offline's, so the dead-stock purge can tell them apart). The stock-only refresh
    writes no product, so it never changes whether a product is hidden: that waits for the full run, which serves
    the product and its stock together. The reason is part of the stock (db.STOCK_DELTA_SQL): hiding or showing a
    product already at stock 0 is served."""
    not_ready = {p["sku"] for p in ledger.execute("SELECT sku FROM product WHERE photo_ready = 0")}
    now = now_iso()
    n = 0
    for r in rows:
        sku = _sku(r, cfg)
        stored = ledger.get("inventory", "sku", sku)
        hidden = sku in not_ready
        reason = "no_photo" if hidden else None
        made_to_order = r.made_to_order and not hidden
        if made_to_order:
            qty = None
        elif hidden:
            qty = 0
        else:
            resolved = inventory_str(r)
            qty = int(resolved) if resolved.isdigit() else 0
        _write_stock(ledger, stored, {
            "sku": sku,
            "qty": qty,
            "made_to_order": 1 if made_to_order else 0,
            # a normal scrape write clears any 'delisted' marker: re-scraping a re-enabled vendor republishes it
            # (restock delta, no reason), and a product whose standardised photo is now published comes back
            "reason": reason,
            "replaced_by_lot_code": None,   # listed again, so no longer replaced by a lot
            "updated_at": now,
        })
        n += 1
    return n


def withdraw_unsent(ledger: Ledger, rows: Iterable[CanonicalRow], cfg: SourceConfig) -> int:
    """Withdraw the unsent change of this source's products waiting to be served (pending or dirty) that this run no
    longer produces: dropped by the supplier, held this run, or found not to be a product (a void slab). Left waiting,
    the next pull would serve stale data and stock, and photos the photos side has since removed (publish drops the
    photos of a listing that left the sheet).
    - Never received by Medusa (not db.in_medusa_sql): the row and its stock are deleted; one that comes back is written
      again as new.
    - Held by Medusa: only the unsent change is withdrawn. The row stays and the delist owns it (stock 0 through
      populate_discontinued, design 5A; never the removed lane, which deletes from Medusa): state synced, its failure
      bookkeeping cleared, payload_hash blank, since the ledger no longer holds the content Medusa acked, so a later
      return to the emit always differs and re-serves as dirty.
    A product in flight ('syncing') is untouched. Returns how many were withdrawn."""
    produced = {_sku(r, cfg) for r in rows}
    stale = [(r["sku"], r["held"]) for r in ledger.execute(
        f"SELECT p.sku, {in_medusa_sql('p')} AS held FROM product p "
        "WHERE p.source = ? AND p.state IN ('pending', 'dirty')", (cfg.source_code,)) if r["sku"] not in produced]
    now = now_iso()
    for sku, held in stale:
        if held:
            ledger.execute("UPDATE product SET state = 'synced', sync_attempts = 0, sync_error = NULL, "
                           "retry_after = NULL, payload_hash = '', updated_at = ? WHERE sku = ?", (now, sku))
        else:
            ledger.execute("DELETE FROM inventory WHERE sku = ?", (sku,))   # the stock row references the product
            ledger.execute("DELETE FROM product WHERE sku = ?", (sku,))
    return len(stale)


def _linked_standard(row) -> set[str]:
    """The object keys of the standardised files a product row links (thumbnail, gallery, oriented faces, 3D)."""
    urls = [row["thumbnail_key"], row["model_3d_key"], *json.loads(row["product_image_keys"] or "[]"),
            *json.loads(row["oriented_image_keys"] or "[]")]
    return {key for url in urls if url and (key := imagestore.standard_object_key(url))}


def mark_unpublished_photos(ledger: Ledger, published: frozenset[str], cfg: SourceConfig) -> list[str]:
    """Record, for every product of this source (emitted this run or not), the standardised files it links that are not
    in `published` (images.published_keys: the folders the photos publish keeps), as photo_unpublished: a sorted JSON
    array, null when every one is published. A product with any is never served (sync._ELIGIBLE_PRODUCT): the produce
    never lets a product serve a photo the photos side has not published. Scraped and improved photos are out of scope.
    Writes only a value that changes (no sync state, no updated_at: it is a content fact, not a sync step). Returns the
    SKUs that have any, sorted."""
    marked = []
    for row in ledger.execute("SELECT sku, thumbnail_key, product_image_keys, oriented_image_keys, model_3d_key, "
                              "photo_unpublished FROM product WHERE source = ?", (cfg.source_code,)).fetchall():
        missing = sorted(_linked_standard(row) - published)
        value = json.dumps(missing) if missing else None
        if value != row["photo_unpublished"]:
            ledger.execute("UPDATE product SET photo_unpublished = ? WHERE sku = ?", (value, row["sku"]))
        if missing:
            marked.append(row["sku"])
    return sorted(marked)


def populate_discontinued(ledger: Ledger, pairs: Iterable[tuple[str, str]],
                          replaced: dict[str, str] | None = None) -> int:
    """Mark products the supplier dropped as out of stock (qty 0), the reversible
    delist (design 5A). `pairs` are the (sku, handle) tuples product_state.discontinued
    produces. Distinct from in-stock quantity (which floors at 1), so a product can
    only reach 0 through this path. A minimal product row is created if absent (the
    dropped product is not in this run's emit), without clobbering an existing one.
    `replaced` (sku -> lot_code) names the dropped products that are bundles replaced by
    their lot: their delist carries reason 'replaced_by_lot' and the code (lots.replacements)."""
    replaced = replaced or {}
    now = now_iso()
    n = 0
    for sku, handle in pairs:
        sku = (sku or "").strip()
        if not sku:
            continue
        src, _, _ = sku.partition("-")
        # a dropped product is one the products export showed, so Medusa holds it by SKU (db.in_medusa_sql)
        ledger.execute(
            "INSERT OR IGNORE INTO product (sku, source, handle, state, last_synced, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sku, src.lower(), handle or "", "synced", now, now, now),
        )
        # a plain drop is dead stock, whatever hid it before (a photo-hidden product its supplier drops is purgeable),
        # and no longer replaced by a lot
        stored = ledger.get("inventory", "sku", sku)
        _write_stock(ledger, stored, {"sku": sku, "qty": 0, "made_to_order": 0,
                                      "reason": "replaced_by_lot" if sku in replaced else None,
                                      "replaced_by_lot_code": replaced.get(sku), "updated_at": now})
        n += 1
    return n
