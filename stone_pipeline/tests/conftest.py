"""Shared test fixtures.

Test isolation from the developer's live config store: the scraper config lives in a durable
`config.db` whose `enabled` flags are edited for ops (e.g. running a single scraper). The pipeline
reads it (`run_all` filters by the enabled set), so without isolation a dev config edit silently
breaks tests -- e.g. disabling a scraper makes a multi-source test see only one source. Point
BLOKPORT_CONFIG_DB at a path that does not exist for every test, so `enabled_names()` returns None
(no filter) and `load_sources()` falls back to the committed sources.yaml -- deterministic, never
coupled to mutable local state. Tests that exercise the store itself set their own path on top.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.settings import SETTINGS


@pytest.fixture(autouse=True)
def _isolate_config_store(monkeypatch, tmp_path_factory):
    absent = tmp_path_factory.mktemp("noconfig") / "absent.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(absent))


# --- CI safety net: skip the integration tests that need the operator's LOCAL data --------------------
# A handful of pipeline tests need the from_medusa export (variants_export.csv) and/or a real scrape under
# data/<source>/ -- BOTH gitignored, so present on a developer's machine but ABSENT in CI. Without this they
# ERROR in CI and turn the whole run red, masking every other (genuine unit) test -- i.e. CI stops being a
# real gate. Skip exactly these when their data is absent, so CI is green-when-clean and still catches a
# regression in everything else; locally (data present) this is a pure no-op and they run as before. When CI
# later fetches/fixtures the data, they run there too with no further change.
_DATA_DEPENDENT_MODULES = {
    "test_m1_reference", "test_m5_variation", "test_m9_overrides_writeback",       # need the variants export
    "test_m8_emit", "test_m12_production", "test_spine_end_to_end", "test_tiles",  # need a real local scrape
}


def _pipeline_data_present() -> bool:
    """True when the operator's local pipeline data is present: the from_medusa variants export AND at
    least one scraped products.csv. Both are gitignored, so this is True locally and False in a bare CI."""
    export = SETTINGS.paths.export_file.exists()
    data_dir = SETTINGS.paths.data_dir
    scrape = data_dir.exists() and any(data_dir.glob("*/*/products.csv"))
    return export and scrape


def pytest_collection_modifyitems(config, items):
    if _pipeline_data_present():
        return
    skip = pytest.mark.skip(reason="needs local from_medusa export + a scrape (gitignored, absent in CI)")
    for item in items:
        if item.module.__name__.rsplit(".", 1)[-1] in _DATA_DEPENDENT_MODULES:
            item.add_marker(skip)
