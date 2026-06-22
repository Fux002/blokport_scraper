"""M11 DoD: each adapter's fixture passes; marenostone runs and routes its
generic-descriptor rows to the gap queue rather than guessing. Each new source
reuses the spine unchanged (no stage touched).
"""

from __future__ import annotations

import glob

import pytest

from stone_pipeline.adapters import selftest
from stone_pipeline.adapters.base import read_scrape_csv
from stone_pipeline.adapters.tokens import extract_color, strip_variety
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.run import run_source


@pytest.mark.parametrize("source", ["polonine", "marenostone", "zucchi", "varsha"])
def test_fixture_selftest_passes(source):
    ok, message = selftest.run_fixture(source)
    assert ok, message


def test_generic_descriptor_yields_empty_variety():
    # a pure colour+type+format descriptor has no named variety
    assert strip_variety("Cream Marble Tile") == ""
    assert strip_variety("Black Granite Slab") == ""
    # a real variety token survives
    assert strip_variety("Pietra Gray Marble Slab") == "Pietra"


def test_color_extracted_from_name():
    assert extract_color("Alaska Gold") == "Gold"
    assert extract_color("Acadian Night is a black granite") == "Black"
    assert extract_color("No colour here") == ""


def test_marenostone_routes_generic_to_gaps_not_guesses(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    manifest = run_source("marenostone", outputs_dir=out, state_dir=out)
    # generic descriptors must not be guessed into output: they gap
    assert manifest.gap_kind_counts.get("GapKind.missing_variation", 0) > 0
    # the spine still emits the rows that DO resolve fully
    assert manifest.totals["emitted"] >= 1
    # no emitted row lacks a variation id (no guess reached output)
    import csv
    path = glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True)[0]
    for row in csv.DictReader(open(path, encoding="utf-8-sig")):
        assert row["STN Variation Id"].strip()


def test_blank_sku_mints_not_drops(tmp_path):
    # marenostone ships blank SKUs; they must mint a surrogate, never drop
    frame = read_scrape_csv(
        SETTINGS.paths.tests_fixtures_dir / "marenostone_products_20260601_155229.csv"
    )
    rows = selftest.REGISTRY["marenostone"].adapt(frame)
    # adapter keeps every row (blank keys included); minting happens in Stage 2
    assert len(rows) == frame.height
