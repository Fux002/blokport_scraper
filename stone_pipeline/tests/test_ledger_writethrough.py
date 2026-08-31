"""Phase 2 write-through, live: a real run with the flag on populates the ledger,
and the ledger's products reproduce the medusa_import.csv the run actually emitted.

This is the live-equivalence proof on real pipeline output (not a fixture): run the
full pipeline for one source, then render products from the shadow ledger and assert
byte-identity with the source's emitted CSV. The flag and the ledger path are set
via env so the live pipeline is only touched when explicitly enabled.
"""

from __future__ import annotations

import glob

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.config.sources import load_source
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.render import render_products
from stone_pipeline.run import run_source

# marenostone scrape data is present locally (gitignored, absent in CI). Unlike the full-equivalence test
# above, the gate tests below spy record_source, so they need only the scrape -- NOT the from_medusa export.
_MAREN_DATA = SETTINGS.paths.data_dir.exists() and any(
    SETTINGS.paths.data_dir.glob("marenostone/*/products.csv"))


def test_writethrough_products_match_emitted_csv(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    ledger_path = tmp_path / "dev.ledger"
    monkeypatch.setenv("BLOKPORT_LEDGER_WRITETHROUGH", "1")
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(ledger_path))

    run_source("polonine", outputs_dir=out, state_dir=out)

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


@pytest.mark.skipif(not _MAREN_DATA, reason="needs local marenostone scrape data (gitignored, absent in CI)")
def test_writethrough_gate_fires_on_neutral_flag(tmp_path, monkeypatch):
    """The run gate consults writethrough.enabled() and, when the NEUTRAL SCRAPER_ flag is set, invokes
    record_source. Verifies the env-prefix consolidation end-to-end through the real run_source gate;
    record_source is spied so the from_medusa export is not required."""
    from stone_pipeline.ledger import writethrough

    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")
    calls: list[int] = []
    monkeypatch.setattr(writethrough, "record_source", lambda *a, **k: (calls.append(1), True)[1])

    run_source("marenostone", outputs_dir=tmp_path / "on", state_dir=tmp_path / "on")
    assert calls == [1], "write-through enabled: the gate must call record_source exactly once"


@pytest.mark.skipif(not _MAREN_DATA, reason="needs local marenostone scrape data (gitignored, absent in CI)")
def test_writethrough_gate_silent_when_disabled(tmp_path, monkeypatch):
    """With neither prefix set, enabled() is False and the gate must not touch the ledger."""
    from stone_pipeline.ledger import writethrough

    monkeypatch.delenv("SCRAPER_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    calls: list[int] = []
    monkeypatch.setattr(writethrough, "record_source", lambda *a, **k: (calls.append(1), True)[1])

    run_source("marenostone", outputs_dir=tmp_path / "off", state_dir=tmp_path / "off")
    assert calls == [], "write-through disabled: the gate must not call record_source"
