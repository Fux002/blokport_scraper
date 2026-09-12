"""Phase 2 write-through, live: a real run with the flag on populates the ledger,
and the ledger's products reproduce the medusa_import.csv the run actually emitted.

This is the live-equivalence proof on real pipeline output (not a fixture): run the
full pipeline for one source, then render products from the shadow ledger and assert
byte-identity with the source's emitted CSV. The flag and the ledger path are set
via env so the live pipeline is only touched when explicitly enabled.
"""

from __future__ import annotations

import csv
import glob

import pytest

from stone_pipeline.config.sources import load_source
from stone_pipeline.ledger import bootstrap
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.reference.loaders import existing_varieties_file
from stone_pipeline.ledger.render import render_products
from stone_pipeline.run import run_source



def _seed_ledger_like_the_sync_server(ledger_path, tmp_path) -> None:
    # The sync server seeds the variation table from the Id-bearing live export. Here the ledger is seeded
    # from the SAME file the matcher's candidate index reads (live export, else the committed base), with
    # the loader's own id rule (Id, else Key): the emitted STN Variation Id and the ledger's medusa_id then
    # come from one source, locally and in CI alike.
    export = tmp_path / "variants_export.csv"
    with existing_varieties_file().open(encoding="utf-8-sig", newline="") as src, \
            export.open("w", encoding="utf-8", newline="") as dst:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=["Id"] + [c for c in reader.fieldnames if c != "Id"])
        writer.writeheader()
        for row in reader:
            row["Id"] = (row.get("Id") or "").strip() or row["Key"]
            writer.writerow(row)
    with Ledger.open(ledger_path, env="development") as ledger:
        bootstrap.seed_attributes(ledger)
        bootstrap.seed_variations(ledger, path=export)


def test_writethrough_products_match_emitted_csv(tmp_path, monkeypatch, scrape_data_dir):
    out = tmp_path / "outputs"
    ledger_path = tmp_path / "dev.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_WRITETHROUGH", "1")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(ledger_path))
    _seed_ledger_like_the_sync_server(ledger_path, tmp_path)

    run_source("polonine", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)

    emitted = glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True)
    assert emitted, "the run produced no medusa_import.csv"
    emitted_csv = emitted[0]

    cfg = load_source("polonine")
    rendered = tmp_path / "rendered.csv"
    with Ledger.open(ledger_path, env="development") as ledger:
        n = render_products(ledger, cfg, rendered)

    assert n > 0, "no products recorded into the ledger by write-through"
    assert rendered.read_bytes() == open(emitted_csv, "rb").read(), (
        "ledger write-through products differ from the emitted medusa_import.csv"
    )


def test_writethrough_gate_fires_on_neutral_flag(tmp_path, monkeypatch, scrape_data_dir):
    """The run gate consults writethrough.enabled() and, when the NEUTRAL SCRAPER_ flag is set, invokes
    record_source. Verifies the env-prefix consolidation end-to-end through the real run_source gate;
    record_source is spied so the from_medusa export is not required."""
    from stone_pipeline.ledger import writethrough

    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    calls: list[int] = []
    monkeypatch.setattr(writethrough, "record_source", lambda *a, **k: (calls.append(1), True)[1])

    run_source("marenostone", outputs_dir=tmp_path / "on", state_dir=tmp_path / "on", data_dir=scrape_data_dir)
    assert calls == [1], "write-through enabled: the gate must call record_source exactly once"


def test_writethrough_failure_fails_the_source_run_after_its_bookkeeping(tmp_path, monkeypatch, scrape_data_dir):
    """A failed record_source means Medusa never receives this source: the run must exit non-zero (run_all
    then omits it from results and `all` returns 1), AFTER the CSVs, diagnostics and steps are written so the
    operator can read why."""
    from stone_pipeline.ledger import writethrough

    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    monkeypatch.setattr(writethrough, "record_source", lambda *a, **k: False)
    out = tmp_path / "fail"
    with pytest.raises(SystemExit):
        run_source("marenostone", outputs_dir=out, state_dir=out, data_dir=scrape_data_dir)
    assert glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True), "the CSVs are still written"
    assert glob.glob(str(out / "**" / "diagnostics" / "health.json"), recursive=True), "diagnostics still written"


def test_writethrough_gate_silent_when_disabled(tmp_path, monkeypatch, scrape_data_dir):
    """With neither prefix set, enabled() is False and the gate must not touch the ledger."""
    from stone_pipeline.ledger import writethrough

    monkeypatch.delenv("SCRAPER_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    calls: list[int] = []
    monkeypatch.setattr(writethrough, "record_source", lambda *a, **k: (calls.append(1), True)[1])

    run_source("marenostone", outputs_dir=tmp_path / "off", state_dir=tmp_path / "off", data_dir=scrape_data_dir)
    assert calls == [], "write-through disabled: the gate must not call record_source"
