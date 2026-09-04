"""Post-reset base fallback (scoped to the matcher).

A factory reset deletes variants_export.csv (lifecycle._prune_stale_medusa_export). Until Blokport
re-exports it after the next pull, load_all() points the matcher's variety index at the Id-free base
instead -- so the matcher keeps recognizing existing varieties rather than mass-minting them all as new.
The subtle part is that the base has no Id column: load_variants must use the Key as the variation_id,
else every base row collapses onto one empty-id slot in by_id and the matcher sees ~1 variety.

The fallback is SCOPED to the matcher (load_all's `variants=` source), NOT the global export_file property,
so emit / tree_build / curate still read paths.export_file and never treat the id-free base as the live
export -- that global change broke the emit/adapter tests and was rejected.
"""
from __future__ import annotations

import csv

from stone_pipeline.reference.loaders import load_variants


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


def test_idfree_base_indexes_by_key_not_empty_id(tmp_path):
    """The base (no Id column) loads every variety, keyed distinctly by its Key -- no empty-id collision."""
    p = tmp_path / "variants_export_base.csv"
    _write(p, ["Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"], [
        ["slab_granite_black_absolut_aaa", "Black Absolut", "", "Absolut Black", ""],
        ["slab_marble_acquamare_bbb", "Acquamare", "", "Verde Acquamare", ""],
    ])
    table = load_variants(p, "slab", key_prefix="slab")
    assert len(table.by_id) == 2  # both loaded, not collapsed onto one empty id
    v = table.by_id["slab_granite_black_absolut_aaa"]
    assert v.variation_id == v.key == "slab_granite_black_absolut_aaa"


def test_live_export_still_uses_the_id(tmp_path):
    """The live export path is unchanged: when an Id is present, it stays the variation_id."""
    p = tmp_path / "variants_export.csv"
    _write(p, ["Id", "Key", "Name", "Image", "Aliases"], [
        ["01MEDUSAID", "slab_granite_black_absolut_aaa", "Black Absolut", "", "Absolut Black"],
    ])
    table = load_variants(p, "slab", key_prefix="slab")
    v = next(iter(table.by_id.values()))
    assert v.variation_id == "01MEDUSAID"
    assert v.key == "slab_granite_black_absolut_aaa"

