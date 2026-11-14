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

import hashlib

import pytest

from stone_pipeline.config.settings import SETTINGS


@pytest.fixture(autouse=True)
def _no_ambient_aws(monkeypatch):
    """Tests run without AWS credentials, exactly as CI does. A developer machine with ~/.aws would otherwise
    let a test reach the real bucket (a call that swallows NoCredentialsError passes here and fails in CI, or
    the reverse), so local and CI runs disagree. Every S3 client a test needs is a fake."""
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(autouse=True)
def _no_ambient_run_brand(monkeypatch):
    """No test inherits a market-price run brand from the shell (under either prefix); a test that runs an entry sets
    the one it means, so a run without it is the default every test sees."""
    for var in ("SCRAPER_MARKET_PRICES_BRAND", "BLOKPORT_MARKET_PRICES_BRAND"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _published_field_kinds():
    """The name-field kinds come from the backend's published field-kinds.json, which a produce fetches. Tests read
    the committed fixture instead (the only place a copy lives), so every writer cleans by a real map. Session-scoped
    so a module-scoped fixture that runs the pipeline sees it too; the cache keys by file hash, so a test that points
    `_path` elsewhere needs no reset."""
    from pathlib import Path
    from stone_pipeline.reference.loaders import field_kinds
    fixture = Path(__file__).parent / "fixtures" / "field-kinds.json"
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(field_kinds, "_path", lambda: fixture)
        yield


@pytest.fixture(autouse=True)
def _isolate_magnitude_baselines(monkeypatch, tmp_path_factory):
    """The drift gate's baseline lives in the developer's gitignored state/ (absent in CI). A stale one there
    (e.g. from before a convention change) makes a run abort before emit and fails tests that never assert on
    drift. Every test gets an empty, private baseline file, so local runs behave like CI."""
    from stone_pipeline.stages import magnitude_drift
    path = tmp_path_factory.mktemp("magnitude") / "magnitude_baselines.json"
    monkeypatch.setattr(magnitude_drift, "_path", lambda: path)


@pytest.fixture(autouse=True, scope="session")
def _isolate_config_store_for_the_session(tmp_path_factory):
    """The same isolation for a module-scoped fixture, which runs before the per-test one below: a pipeline run there
    proposes its learned aliases into config.db (P2-10 C2), and must never reach the checkout's own config.db (CI's
    `certify all` then read that store and found no sources)."""
    absent = tmp_path_factory.mktemp("noconfig_session") / "absent.db"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("BLOKPORT_CONFIG_DB", str(absent))
        yield


@pytest.fixture(autouse=True)
def _isolate_config_store(monkeypatch, tmp_path_factory):
    absent = tmp_path_factory.mktemp("noconfig") / "absent.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(absent))


@pytest.fixture
def seeded_config_store(monkeypatch, tmp_path_factory):
    """A private config.db seeded from the committed sources.yaml, as the config server seeds it at boot. For a test
    that runs several sources in one process: a run proposes its learned guesses into config.db (P2-10 C2), and a
    store first created by that write would hold no sources, so the next source's load_source would find none."""
    from stone_pipeline.config import store
    path = tmp_path_factory.mktemp("config") / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    store.seed_from_yaml()
    return path


@pytest.fixture
def tree_category_pcats(monkeypatch):
    """The tree-build tests' category pcats on the registry (table 577: a category's pcat is the registry's)."""
    from stone_pipeline.tests._category_registry import TREE_PCATS, set_category_pcats
    set_category_pcats(monkeypatch, TREE_PCATS)


@pytest.fixture
def ledger_product_pcats(monkeypatch):
    """test_ledger_products' synthetic category pcats (PC1 slabs, PC2 blocks) on the registry (table 577)."""
    from stone_pipeline.tests._category_registry import LEDGER_PRODUCT_PCATS, set_category_pcats
    set_category_pcats(monkeypatch, LEDGER_PRODUCT_PCATS)


@pytest.fixture
def slab_category_pcat(monkeypatch):
    """The Slabs category on SLAB_PCAT, so a canonical row on that pcat is stored under `slabs` (table 577)."""
    from stone_pipeline.tests._category_registry import set_category_pcats
    from stone_pipeline.tests._ledger_icons import SLAB_PCAT
    set_category_pcats(monkeypatch, {"slab": SLAB_PCAT})


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


class _EveryUrlImproved(dict):
    """A passthrough imageproc manifest that knows every source url: each maps to its own deterministic
    improved/ url, so the image stage links every product (the real manifest lives on S3)."""

    def get(self, url, default=None):
        from stone_pipeline.io import imagestore
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return f"https://fixtures.invalid{imagestore.IMPROVED_MARKER}test/{digest}.png"

    def __bool__(self):
        return True


@pytest.fixture(scope="module")
def scrape_data_dir():
    """The hermetic pipeline input: the committed scrape fixtures, laid out exactly like the workspace
    data/ tree (<source>/<timestamp>/products.csv + scrape_complete.json), plus an image stage that needs
    no S3 (the stub the image unit tests use: a manifest that knows every url). run_source / run_all then read the SAME input locally and in CI: no test depends on the
    operator's gitignored scrapes or on the dev bucket's manifest."""
    from stone_pipeline.stages import images
    with pytest.MonkeyPatch.context() as mp:            # module scope: the m8 emitted fixture runs once per module
        mp.setattr(images, "_readonly_manifest", _EveryUrlImproved)
        yield SETTINGS.paths.tests_fixtures_dir / "data"
