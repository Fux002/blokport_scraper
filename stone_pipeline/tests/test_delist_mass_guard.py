"""The mass-delist guard in run_source refuses a delist that would take down more than
delist_max_fraction of a source's LIVE catalogue (a partial scrape). Products Medusa already holds at
stock 0 are sold history: counting them made the guard refuse every delist once the history outgrew the
live catalogue, so sold slabs stayed listed with stock (prod zucchi: 881 of 2618 'gone', only 357 live).
"""

from __future__ import annotations

import csv

import pytest

from stone_pipeline.run import run_source


def _skus(path, column="Variant Sku") -> set[str]:
    with open(path, encoding="utf-8-sig") as h:
        return {r[column] for r in csv.DictReader(h)}


def _export(path, rows):
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        w.writerow(["Product Handle", "Variant Sku", "Inventory Quantity"])
        for sku, qty in rows:
            w.writerow([f"h-{sku.lower()}", sku, qty])
    return path


def _run(tmp_path, name, scrape_data_dir, export):
    out = tmp_path / name
    m = run_source("marenostone", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir,
                   known_products_path=export)
    return out / m.run_id


def test_sold_history_does_not_block_a_real_delist(tmp_path, scrape_data_dir, caplog):
    first = _run(tmp_path, "first", scrape_data_dir, _export(tmp_path / "none.csv", []))
    live = sorted(_skus(first / "4_products_import" / "medusa_import.csv"))
    assert len(live) >= 10
    gone_live = [("MAR-GONE-1", 3)]                                   # really dropped by the supplier
    sold = [(f"MAR-SOLD-{i}", 0) for i in range(2 * len(live))]       # history outweighs the live catalogue
    export = _export(tmp_path / "known.csv", [(s, 2) for s in live] + gone_live + sold)

    second = _run(tmp_path, "second", scrape_data_dir, export)

    assert not any("delist refused" in r.getMessage() for r in caplog.records)
    assert _skus(second / "review" / "products_discontinued.csv") == {"MAR-GONE-1"} | {s for s, _ in sold}


def test_a_partial_scrape_is_still_refused(tmp_path, scrape_data_dir, caplog):
    first = _run(tmp_path, "first", scrape_data_dir, _export(tmp_path / "none.csv", []))
    live = sorted(_skus(first / "4_products_import" / "medusa_import.csv"))
    missing = [(f"MAR-MISSING-{i}", 2) for i in range(len(live))]    # half the live catalogue absent
    export = _export(tmp_path / "known.csv", [(s, 2) for s in live] + missing)

    second = _run(tmp_path, "second", scrape_data_dir, export)

    assert any("delist refused" in r.getMessage() for r in caplog.records)
    assert _skus(second / "review" / "products_discontinued.csv") == set()


def test_a_mistyped_collection_country_holds_the_source_and_keeps_the_last_good_run(tmp_path, scrape_data_dir, monkeypatch, caplog):
    import dataclasses
    from stone_pipeline import run as run_module
    from stone_pipeline.config.sources import load_source
    out = tmp_path / "outputs"
    good = dataclasses.replace(load_source("marenostone"), collection_country_default="IT", collection_city_default="Verona")
    monkeypatch.setattr(run_module, "load_source", lambda name: good)
    run_source("marenostone", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    before = {p: p.read_bytes() for p in out.rglob("canonical.parquet")}
    assert before
    typo = dataclasses.replace(good, collection_country_default="Itly")
    monkeypatch.setattr(run_module, "load_source", lambda name: typo)
    with pytest.raises(SystemExit) as stopped, caplog.at_level("ERROR"):
        run_source("marenostone", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    assert stopped.value.code == 2
    assert "Itly" in caplog.text
    assert {p: p.read_bytes() for p in out.rglob("canonical.parquet")} == before
