"""BLOKPORT_ENV is the deployment TIER and nothing else.

Every production guard in config.settings keys off IS_PRODUCTION. Before this was validated, an
unrecognised BLOKPORT_ENV merely failed to look like production and the whole module silently fell
back to DEVELOPMENT semantics: dev S3 prefix, dry-run defaulting on, and the bucket + sales-channel
guards disabled. A brand-prefixed value like "wudport-production" is the obvious way to hit that,
and it fails silently and expensively -- a prod run that looks healthy while writing to dev paths.

These tests pin the closed tier set, so the guard cannot regress into a silent default.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_settings(monkeypatch, value):
    """Re-import config.settings with BLOKPORT_ENV set, since the tier is resolved at import."""
    if value is None:
        monkeypatch.delenv("BLOKPORT_ENV", raising=False)
    else:
        monkeypatch.setenv("BLOKPORT_ENV", value)
    # Production additionally requires an explicit bucket, so satisfy that guard: it is a
    # DIFFERENT check and must not be what makes these cases pass or fail.
    monkeypatch.setenv("BLOKPORT_S3_BUCKET", "test-bucket")
    import stone_pipeline.config.settings as settings
    return importlib.reload(settings)


@pytest.mark.parametrize("value,expected_env,expected_prod", [
    (None, "development", False),          # unset default
    ("development", "development", False),
    ("dev", "development", False),         # alias normalises
    ("production", "production", True),
    ("prod", "production", True),          # alias normalises
    ("  Production  ", "production", True),  # whitespace + case tolerated
])
def test_valid_tiers_resolve_canonically(monkeypatch, value, expected_env, expected_prod):
    s = _reload_settings(monkeypatch, value)
    assert s.BLOKPORT_ENV == expected_env      # canonical, never the raw alias
    assert s.IS_PRODUCTION is expected_prod
    assert s.ENV_NAME == expected_env
    assert s.ENV_SEGMENT == ("prod" if expected_prod else "dev")


@pytest.mark.parametrize("value", [
    "wudport-production",   # the brand-prefixed trap this guard exists for
    "wudport-prod",
    "blokport-production",
    "staging",
    "PRODUCTIONN",
    "",
])
def test_unknown_tier_fails_loud_instead_of_silently_becoming_dev(monkeypatch, value):
    with pytest.raises(RuntimeError) as e:
        _reload_settings(monkeypatch, value)
    msg = str(e.value)
    # The error must say what is wrong AND what to do instead, since the failure mode it replaces
    # was silent: a second brand gets its own DEPLOYMENT, not a new tier string.
    assert "BLOKPORT_ENV" in msg
    assert "BLOKPORT_DOMAIN_PACK" in msg


def test_tier_is_not_a_brand_slot(monkeypatch):
    """The regression in one line: a brand-prefixed tier must never read as development."""
    with pytest.raises(RuntimeError):
        _reload_settings(monkeypatch, "wudport-production")


@pytest.fixture(autouse=True)
def _restore_settings(monkeypatch):
    """Leave config.settings reloaded at its default so later tests see the normal module."""
    yield
    monkeypatch.delenv("BLOKPORT_ENV", raising=False)
    monkeypatch.delenv("BLOKPORT_S3_BUCKET", raising=False)
    import stone_pipeline.config.settings as settings
    importlib.reload(settings)
