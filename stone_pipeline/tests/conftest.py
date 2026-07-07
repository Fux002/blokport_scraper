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


@pytest.fixture(autouse=True)
def _clear_vocab_caches():
    """The colour/type recognition vocab is lazily loaded and lru-cached from attributes.csv + the synonym
    files. Clear those caches around every test so one that monkeypatches those paths can neither leak a
    stale vocab into another test nor inherit one."""
    from stone_pipeline.adapters import tokens
    from stone_pipeline.reference import loaders
    caches = (tokens.known_values, tokens._clear_type_words, tokens._colour_lookup,
              tokens._strip_tokens, loaders.load_synonyms)
    for cache in caches:
        cache.cache_clear()
    yield
    for cache in caches:
        cache.cache_clear()


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

# Modules that are MOSTLY pure unit tests but hold a FEW export/scrape-dependent tests. Skipping the whole
# module (above) would drop real unit coverage (e.g. test_m11_adapters is 16 tests, one needs a scrape;
# test_curate is 13, two need the export), so skip JUST the data-dependent tests by name and keep the rest
# running as a real CI gate. Node id = "module::test_name". Extend as new such tests land.
_DATA_DEPENDENT_TESTS = {
    "test_curate::test_alias_addition_preserves_and_augments",
    "test_curate::test_borderline_gap_becomes_alias_candidate_not_new_variant",
    "test_curate::test_alias_decision_routes_spelling_onto_target_and_mints_nothing",
    "test_inventory::test_inventory_only_run_skips_images_products_and_canonical",
    "test_ledger_writethrough::test_writethrough_products_match_emitted_csv",
    "test_m11_adapters::test_marenostone_routes_generic_to_gaps_not_guesses",
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
        mod = item.module.__name__.rsplit(".", 1)[-1]
        if mod in _DATA_DEPENDENT_MODULES or f"{mod}::{item.originalname}" in _DATA_DEPENDENT_TESTS:
            item.add_marker(skip)
