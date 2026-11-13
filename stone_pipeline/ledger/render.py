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

from stone_pipeline.config.categories import category_by_handle
from stone_pipeline.config.sources import SourceConfig
from stone_pipeline.core import csvio
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.ledger.bootstrap import attribute_id
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.reference.medusa_files import CATEGORY_KIND
from stone_pipeline.stages import emit
from stone_pipeline.stages.emit_catalog import _COLS as VARIANTS_FULL_COLS


def _aliases_cell(stored: str | None) -> str:
    """The alias_rows JSON (ledger form) -> the pipe-joined values the variants file's Aliases cell holds (empty when
    not computed yet)."""
    if not stored:
        return ""
    return "|".join(r["value"] for r in json.loads(stored))


def render_variants_full(ledger: Ledger, path: str | Path) -> int:
    """Render the `variation` table into a 1_variants_full.csv-shaped file.

    Rows are emitted in insertion order (rowid), which is the source file order when
    the ledger was loaded from that file, so a round-trip reproduces row order. The
    dict keys are the producer's column names, so the header can never drift from it.
    """
    key_col, name_col, type_col, aliases_col = VARIANTS_FULL_COLS
    rows = [{
        key_col: v["key"],
        name_col: v["name"],
        type_col: v["type"],
        aliases_col: _aliases_cell(v["alias_rows"]),
    } for v in ledger.execute(
        "SELECT key, name, type, alias_rows FROM variation "
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
        category_pcat_id=_category_pcat(p["category"]) if p["category"] else None,
        title=p["title"], description=p["description"], handle=p["handle"], slug=p["slug"],
        weight=p["weight"], length=p["length"], width=p["width"], height=p["height"],
        origin_country_code=p["origin_country_code"],
        origin_city=p["origin_city"], origin_county=p["origin_county"],
        thumbnail_key=p["thumbnail_key"],
        oriented_image_keys=json.loads(p["oriented_image_keys"] or "[]"),
        product_image_keys=json.loads(p["product_image_keys"] or "[]"),
        model_3d_key=p["model_3d_key"],
        company_id=p["company_id"], sales_channel_id=p["sales_channel_id"],
        port_ids=json.loads(p["ports"] or "[]"),
        visibility=p["visibility"], discountable=p["discountable"],
        sold_in_bundle=bool(p["sold_in_bundle"]),
        bundle_size=p["bundle_size"],
        # emit reads the derived inventory_quantity field; set it directly from the stored stock so a
        # ledger-render re-emit reproduces the same Variant Inventory Quantity cell (a rebuilt row skips
        # Stage 6, so nothing else would populate it). Made to order carries no count.
        made_to_order=bool(p["made_to_order"]),
        inventory_quantity=_stored_count(p),
    )


def _category_pcat(handle: str) -> str | None:
    """The pcat of a stored category HANDLE (D11), from the category registry (table 577: the one source of a
    category; the ledger's attribute table holds no category rows). An unknown handle raises: a product is never
    rendered under a guessed category."""
    if (cat := category_by_handle(handle)) is None:
        raise ValueError(f"category handle {handle!r} is no pack category")
    return cat.pcat_id or None


def _stored_count(p) -> int | None:
    """A product row's stored stock count: none when made to order, else its stock string as an int (0 when blank)."""
    if p["made_to_order"]:
        return None
    text = str(p["inventory_quantity"] or "").strip()
    return int(text) if text.lstrip("-").isdigit() else 0


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


# --- combinations (a sync-render, not a round-trip) ---------------------------
#
# 2_valid_combinations.csv is a pure function of the variation ids (export) and
# attribute ids, plus the backbone tree and products sheet (external reference
# data). Both id sources live in the ledger after bootstrap, so the render dumps
# them back into the shapes build_combinations reads and reuses that builder
# unchanged. Identical id maps in -> identical sorted combination set out.

def _dump_attributes(ledger: Ledger, path: Path) -> None:
    """Ledger attribute table -> attributes.csv shape, WITH status/internal (VE2). The ledger does not store
    those (the files are the single authority, PR3/PR4 ruling), so they are sourced from the real
    attributes.csv per (category, value); a value absent there defaults to active/not-internal. Carrying them
    is what makes build_combinations filter the RENDER'S combinations exactly as the real build does, so
    ledger.verify still matches once a value is archived. Dormant: with no status columns every value reads
    active/false, so the combination set is byte-identical to today. Category rows are never written: a category
    is the registry's (table 577), and any a live ledger still holds from before are inert, so they are skipped."""
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.reference.loaders.common import norm
    from stone_pipeline.reference.medusa_files import read_attributes
    src = SETTINGS.paths.attributes_csv
    status_of: dict[tuple[str, str], tuple[str, str]] = {}
    if src.exists():
        for a in read_attributes(src, require_status=False):
            status_of[(a.category, norm(a.value))] = (a.status, "true" if a.internal else "false")
    with Path(path).open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["category", "value", "sourceid", "status", "internal", "handle"])
        for r in ledger.execute("SELECT category, value, medusa_id FROM attribute WHERE category != ?",
                                (CATEGORY_KIND,)):
            status, internal = status_of.get((r["category"], norm(r["value"])), ("active", "false"))
            w.writerow([r["category"], r["value"], r["medusa_id"] or "", status, internal, ""])


def _dump_variations_export(ledger: Ledger, path: Path) -> None:
    """Ledger variation table -> variants_export.csv shape (Id plus the variety file's columns). build_combinations
    reads Id, Key and Name; the rest fill the header."""
    with Path(path).open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Id", *VARIANTS_FULL_COLS])
        for r in ledger.execute("SELECT medusa_id, key, name, type, alias_rows FROM variation"):
            w.writerow([r["medusa_id"] or "", r["key"], r["name"] or "", r["type"], _aliases_cell(r["alias_rows"])])


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
            acked_ledger=ledger,   # VE2: rebuild acked tuples from the ledger verify was given, not the default
        )
    current = {tuple(str(x) for x in c) for c in combinations}
    return tree_build.write_combinations(current, Path(path))
