"""The :4200 list enrichment: what's in the scraper (raw scrape age + rows) and what's produced."""

from __future__ import annotations

import types

from stone_pipeline.config import scrape_status


def _write_scrape(data_dir, source, stamp, n_rows):
    d = data_dir / source / stamp
    d.mkdir(parents=True)
    lines = ["col_a,col_b"] + [f"v{i},w{i}" for i in range(n_rows)]
    (d / "products.csv").write_text("\n".join(lines), encoding="utf-8")


def _use_data_dir(monkeypatch, tmp_path):
    # Paths is a frozen dataclass, so swap the module's SETTINGS for a stand-in with our data_dir.
    monkeypatch.setattr(scrape_status, "SETTINGS",
                        types.SimpleNamespace(paths=types.SimpleNamespace(data_dir=tmp_path)))


def test_scrape_info_reports_latest_scrape_age_and_rows(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    _write_scrape(tmp_path, "zucchi", "20260601_090000", 5)
    _write_scrape(tmp_path, "zucchi", "20260625_222931", 2062)     # newer -> this one wins
    info = scrape_status.scrape_info("zucchi")
    assert info["scrape_at"] == "2026-06-25T22:29:31"
    assert info["scrape_rows"] == 2062


def test_scrape_info_null_when_never_scraped(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    assert scrape_status.scrape_info("polonine") == {"scrape_at": None, "scrape_rows": None}


def test_enrich_adds_scrape_and_ledger_fields(tmp_path, monkeypatch):
    _use_data_dir(monkeypatch, tmp_path)
    _write_scrape(tmp_path, "polonine", "20260621_104529", 303)
    monkeypatch.setattr(scrape_status, "ledger_product_counts", lambda: {"pol": 241})
    rows = scrape_status.enrich([{"source": "polonine", "source_code": "pol"},
                                 {"source": "zucchi", "source_code": "zuc"}])
    pol = next(r for r in rows if r["source"] == "polonine")
    zuc = next(r for r in rows if r["source"] == "zucchi")
    assert pol["scrape_at"] == "2026-06-21T10:45:29" and pol["scrape_rows"] == 303
    assert pol["ledger_products"] == 241
    assert zuc["scrape_at"] is None and zuc["ledger_products"] == 0   # never scraped, none produced
