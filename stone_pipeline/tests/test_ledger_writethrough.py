"""Phase 2 write-through, live: a real run with the flag on populates the ledger,
and the ledger's products reproduce the medusa_import.csv the run actually emitted.

This is the live-equivalence proof on real pipeline output (not a fixture): run the
full pipeline for one source, then render products from the shadow ledger and assert
byte-identity with the source's emitted CSV. The flag and the ledger path are set
via env so the live pipeline is only touched when explicitly enabled.
"""

from __future__ import annotations

import glob

from stone_pipeline.config.sources import load_source
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.ledger.render import render_products
from stone_pipeline.run import run_source


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
