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
def _isolate_magnitude_baselines(monkeypatch, tmp_path_factory):
    """The drift gate's baseline lives in the developer's gitignored state/ (absent in CI). A stale one there
    (e.g. from before a convention change) makes a run abort before emit and fails tests that never assert on
    drift. Every test gets an empty, private baseline file, so local runs behave like CI."""
    from stone_pipeline.stages import magnitude_drift
    path = tmp_path_factory.mktemp("magnitude") / "magnitude_baselines.json"
    monkeypatch.setattr(magnitude_drift, "_path", lambda: path)


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
    no S3 (the same two stubs the image unit tests use: a manifest that knows every url, an empty discard
    pool). run_source / run_all then read the SAME input locally and in CI: no test depends on the
    operator's gitignored scrapes or on the dev bucket's manifest."""
    from stone_pipeline.stages import images
    with pytest.MonkeyPatch.context() as mp:            # module scope: the m8 emitted fixture runs once per module
        mp.setattr(images, "_readonly_manifest", _EveryUrlImproved)
        mp.setattr(images, "_load_discard_set", lambda cfg=None: set())
        yield SETTINGS.paths.tests_fixtures_dir / "data"
