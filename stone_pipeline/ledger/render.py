"""Render ledger entities back into the to_upload CSV shapes.

This is the other half of the Phase 1 cutover test (SYNC_LEDGER_DESIGN.md section
13): render the ledger and assert it reproduces the existing CSV byte-for-byte.

Byte-identity is guaranteed by construction: rendering reuses the pipeline's own
writer (`core.csvio.write_dicts`) and the SAME column list the producer uses, so
the equivalence test is really checking one thing only, that the ledger preserved
the cell values and their order. No em dashes (design principle 2).
"""

from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import csvio
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.bootstrap import attribute_id
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.stages import emit
from stone_pipeline.stages.emit_catalog import _COLS as VARIANTS_FULL_COLS


def _aliases_cell(stored: str | None) -> str:
    """JSON array (ledger form) -> the pipe-joined cell the variants file uses."""
    if not stored:
        return ""
    return "|".join(json.loads(stored))


def render_variants_full(ledger: Ledger, path: str | Path) -> int:
    """Render the `variation` table into a 1_variants_full.csv-shaped file.

    Rows are emitted in insertion order (rowid), which is the source file order when
    the ledger was loaded from that file, so a round-trip reproduces row order. The
    dict keys are the imported column names, so the header (including the unicode in
    "Volume per kg (m3/kg)") can never drift from the producer's.
    """
    key_col, name_col, image_col, aliases_col, volume_col = VARIANTS_FULL_COLS
    rows = [{
        key_col: v["key"],
        name_col: v["name"],
        image_col: v["image_url"] or "",
        aliases_col: _aliases_cell(v["aliases"]),
        volume_col: v["volume"] or "",
    } for v in ledger.execute(
        "SELECT key, name, image_url, aliases, volume FROM variation "
        "WHERE in_full = 1 ORDER BY rowid")]
    return csvio.write_dicts(path, VARIANTS_FULL_COLS, rows, sanitize=False)


def _product_shim(ledger: Ledger, p) -> CanonicalRow:
    """Reconstruct a CanonicalRow from a ledger product row, resolving ids by name
    and Key through the seeded tables. emit reads only these fields, so feeding the
    shim back through emit reproduces the row's cells exactly."""
    variation_id = None
    if p["variation_key"]:
        vr = ledger.get("variation", "key", p["variation_key"])
        variation_id = vr["medusa_id"] if vr else None
    return CanonicalRow(
        src_site=p["source"],
        surrogate_key=p["surrogate_key"],
        color_name=p["color"],
        color_id=attribute_id(ledger, "color", p["color"]) if p["color"] else None,
        finish_name=p["finish"],
        finish_id=attribute_id(ledger, "finish", p["finish"]) if p["finish"] else None,
        quality_name=p["quality"],
        quality_id=attribute_id(ledger, "quality", p["quality"]) if p["quality"] else None,
        type_name=p["type"],
        type_id=attribute_id(ledger, "type", p["type"]) if p["type"] else None,
        variation_id=variation_id,
        category_pcat_id=attribute_id(ledger, "category", p["category"]) if p["category"] else None,
        title=p["title"], description=p["description"], handle=p["handle"], slug=p["slug"],
        weight=p["weight"], length=p["length"], width=p["width"], height=p["height"],
        origin_country_code=p["origin_country_code"],
        origin_city=p["origin_city"], origin_county=p["origin_county"],
        thumbnail_key=p["thumbnail_key"],
        oriented_image_keys=json.loads(p["oriented_image_keys"] or "[]"),
        product_image_keys=json.loads(p["product_image_keys"] or "[]"),
        company_id=p["company_id"], sales_channel_id=p["sales_channel_id"],
        port_ids=json.loads(p["ports"] or "[]"),
        visibility=p["visibility"], discountable=p["discountable"],
        sold_in_bundle=bool(p["sold_in_bundle"]),
        bundle_size=p["bundle_size"],
        # emit reads the derived inventory_quantity field; set it directly from the stored stock so a
        # ledger-render re-emit reproduces the same Variant Inventory Quantity cell (a rebuilt row skips
        # Stage 6, so nothing else would populate it).
        inventory_quantity=(int(_q) if (_q := str(p["inventory_quantity"] or "").strip()).lstrip("-").isdigit() else 0),
    )


def render_products(ledger: Ledger, cfg: SourceConfig, path: str | Path,
                    columns: list[str] | None = None, *, source: str | None = None) -> int:
    """Render the `product` table into a medusa_import.csv-shaped file, by feeding
    reconstructed canonical rows back through emit (same column map, same writer).

    Only real, emittable products (those with a variety link) are rendered; minimal
    rows bootstrapped from products_export for the inventory FK carry no
    variation_key. Pass `source` to render one source's products (the SKU is
    recomputed from `cfg.source_code`, so a multi-source render must be done per
    source with the matching cfg)."""
    columns = columns or emit.read_template_columns()
    sql = "SELECT * FROM product WHERE variation_key IS NOT NULL"
    params: list = []
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY rowid"
    shims = [_product_shim(ledger, p) for p in ledger.execute(sql, params)]
    emit.write_import_csv(shims, cfg, Path(path), columns)
    return len(shims)


def render_inventory(ledger: Ledger, cfg: SourceConfig, path: str | Path) -> int:
    """Render the inventory delta (4_inventory_update.csv): the products whose stock
    moved since the last sync (qty distinct from last_synced_qty), joined to the
    product row for the handle. Reuses emit.write_inventory_csv so the format
    matches. A qty of 0 is the reversible delist, written like any other row."""
    served = ledger.execute(
        "SELECT p.sku AS sku, i.qty AS qty, p.surrogate_key AS surrogate_key, p.handle AS handle "
        "FROM inventory i JOIN product p ON p.sku = i.sku "
        "WHERE i.last_synced_qty IS NULL OR i.last_synced_qty != i.qty "
        "ORDER BY i.rowid"
    ).fetchall()
    # qty 0 is the reversible delist: emit writes 0 only via its discontinued path, so split by qty and
    # feed the positive-stock rows through `changed`.
    changed = [
        CanonicalRow(
            src_site=cfg.source_code,
            surrogate_key=r["surrogate_key"],
            handle=r["handle"],
            inventory_quantity=int(r["qty"]),  # emit reads the derived stock field directly
        )
        for r in served if r["qty"] and int(r["qty"]) > 0
    ]
    discontinued = tuple(
        (r["sku"], r["handle"] or "") for r in served if not (r["qty"] and int(r["qty"]) > 0)
    )
    emit.write_inventory_csv(changed, cfg, Path(path), discontinued=discontinued)
    return len(changed) + len(discontinued)


# --- combinations (a sync-render, not a round-trip) ---------------------------
#
# 2_valid_combinations.csv is a pure function of the variation ids (export) and
# attribute ids, plus the backbone tree and products sheet (external reference
# data). Both id sources live in the ledger after bootstrap, so the render dumps
# them back into the shapes build_combinations reads and reuses that builder
# unchanged. Identical id maps in -> identical sorted combination set out.

def _dump_attributes(ledger: Ledger, path: Path) -> None:
    """Ledger attribute table -> attributes.csv shape (category, value, sourceid)."""
    with Path(path).open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["category", "value", "sourceid"])
        for r in ledger.execute("SELECT category, value, medusa_id FROM attribute"):
            w.writerow([r["category"], r["value"], r["medusa_id"] or ""])


def _dump_variations_export(ledger: Ledger, path: Path) -> None:
    """Ledger variation table -> variants_export.csv shape. build_combinations reads
    Id, Key and Name; the rest fill the header."""
    with Path(path).open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"])
        for r in ledger.execute(
            "SELECT medusa_id, key, name, image_url, aliases, volume FROM variation"
        ):
            aliases = "|".join(json.loads(r["aliases"])) if r["aliases"] else ""
            w.writerow([r["medusa_id"] or "", r["key"], r["name"] or "",
                        r["image_url"] or "", aliases, r["volume"] or ""])


def render_combinations(ledger: Ledger, path: str | Path, *,
                        backbone_paths: list[Path] | None = None,
                        products_csv: Path | None = None,
                        assigned_types: dict[str, str] | None = None,
                        exclude_ids: set[str] | None = None) -> int:
    """Build the full valid-combination set from the ledger and write it (sorted),
    reusing stages.tree_build.build_combinations with the export and attributes
    sourced from the ledger instead of the from_medusa CSVs."""
    from stone_pipeline.stages import tree_build

    with tempfile.TemporaryDirectory() as tmp:
        attrs_p = Path(tmp) / "attributes.csv"
        export_p = Path(tmp) / "export.csv"
        _dump_attributes(ledger, attrs_p)
        _dump_variations_export(ledger, export_p)
        combinations, _stats, _uncovered = tree_build.build_combinations(
            export_p, attrs_p,
            backbone_paths if backbone_paths is not None else tree_build._backbone_paths(),
            products_csv, assigned_types, exclude_ids,
        )
    current = {tuple(str(x) for x in c) for c in combinations}
    return tree_build.write_combinations(current, Path(path))
