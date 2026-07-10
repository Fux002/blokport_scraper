"""Phase 2a: any data source is first-class, not just a web scrape. A source declares how it ACQUIRES its
frame (scraper | load_frame | file_drop); the pipeline treats them uniformly, and a load_frame source that
yields no data this run is a CLEAN skip, never the missing-scrape error. The all-scraper path is unchanged.
"""

from __future__ import annotations

import stone_pipeline.adapters as adapter_registry
from stone_pipeline.adapters.base import ACQ_LOAD_FRAME, ACQ_SCRAPER, AdapterBase
from stone_pipeline import produce
from stone_pipeline import run as run_mod


# --- the acquisition discriminator --------------------------------------------
def test_scraper_source_is_the_default():
    # no registered adapter overrides load_frame today, so every existing source stays a 'scraper' source
    # (the CSV path is byte-identical) -- the discriminator adds capability without changing behaviour.
    for source, adapter in adapter_registry.REGISTRY.items():
        assert adapter.acquires_via() == ACQ_SCRAPER, source


def test_load_frame_override_is_inferred():
    class ApiAdapter(AdapterBase):
        source = "toy_api"
        def load_frame(self, scrape_path=None):
            return None
    assert ApiAdapter().acquires_via() == ACQ_LOAD_FRAME       # inferred from the override


def test_explicit_acquisition_wins():
    a = AdapterBase()
    a.acquisition = "file_drop"
    assert a.acquires_via() == "file_drop"


# --- load_frame source with no data is a clean skip ---------------------------
def test_load_frame_no_data_is_a_clean_skip(tmp_path, monkeypatch):
    # a declared load_frame source whose fetch returns None this run must NOT fall through to the CSV path
    # (FileNotFoundError) -- it is a recorded no-op skip, so run_all keeps it out of the failed set.
    adapter = adapter_registry.REGISTRY["marenostone"]
    monkeypatch.setattr(adapter, "acquisition", ACQ_LOAD_FRAME)
    monkeypatch.setattr(adapter, "load_frame", lambda scrape_path=None: None)
    m = run_mod.run_source("marenostone", outputs_dir=tmp_path, state_dir=tmp_path)
    assert m.health_status == run_mod.SKIPPED and m.totals["rows"] == 0


# --- produce dispatches by acquisition ----------------------------------------
def _spy_scraper_main(monkeypatch):
    """Replace scrapers.run.main with a spy that records its args and returns rc 0."""
    seen = {"calls": 0, "srcs": None}
    def fake_main(srcs):
        seen["calls"] += 1
        seen["srcs"] = srcs
        return 0
    monkeypatch.setattr("scrapers.run.main", fake_main)
    return seen


def test_live_scrape_sends_only_scraper_sources(monkeypatch):
    seen = _spy_scraper_main(monkeypatch)
    marenostone = adapter_registry.REGISTRY["marenostone"]
    monkeypatch.setattr(marenostone, "acquisition", ACQ_LOAD_FRAME)   # a load_frame source
    rc = produce._live_scrape(["marenostone", "polonine"])            # polonine stays a scraper
    assert rc == 0 and seen["srcs"] == ["polonine"]                   # load_frame source not sent to scraper


def test_live_scrape_all_load_frame_is_a_noop(monkeypatch):
    seen = _spy_scraper_main(monkeypatch)
    marenostone = adapter_registry.REGISTRY["marenostone"]
    monkeypatch.setattr(marenostone, "acquisition", ACQ_LOAD_FRAME)
    assert produce._live_scrape(["marenostone"]) == 0 and seen["calls"] == 0   # nothing to live-scrape


def test_live_scrape_all_sources_unchanged(monkeypatch):
    # sources=None -> the scraper runner still gets ['all'] exactly as before (no behaviour change).
    seen = _spy_scraper_main(monkeypatch)
    assert produce._live_scrape(None) == 0 and seen["srcs"] == ["all"]
