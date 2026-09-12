"""Wave 3a: config values fail loud instead of silently becoming something else.

A numeric override that is not a number, an image mode that is not one of the three modes, and a corrupt
scrape completion marker each used to be swallowed into a default (150.0, passthrough, "complete and
healthy"). Each is the no_image / silent-delist incident class: the run proceeds on a value nobody set.
"""

from __future__ import annotations

import json

import pytest

from stone_pipeline import run as run_mod
from stone_pipeline.config import settings


def test_a_non_numeric_env_override_fails_loud_not_default(monkeypatch):
    monkeypatch.setenv("SCRAPER_TEST_INT", "abc")
    monkeypatch.setenv("SCRAPER_TEST_FLOAT", "1,5")
    with pytest.raises(ValueError, match="SCRAPER_TEST_INT"):
        settings._env_int("SCRAPER_TEST_INT", 7)
    with pytest.raises(ValueError, match="SCRAPER_TEST_FLOAT"):
        settings._env_float("SCRAPER_TEST_FLOAT", 1.0)


def test_an_unset_env_override_still_yields_the_default(monkeypatch):
    monkeypatch.delenv("SCRAPER_TEST_INT", raising=False)
    monkeypatch.delenv("BLOKPORT_TEST_INT", raising=False)
    assert settings._env_int("SCRAPER_TEST_INT", 7) == 7
    monkeypatch.setenv("SCRAPER_TEST_INT", " 12 ")
    assert settings._env_int("SCRAPER_TEST_INT", 7) == 12


@pytest.mark.parametrize("mode", ["passthru", "S3", "s3 ", ""])
def test_an_unknown_image_mode_is_rejected_at_config_time(mode):
    with pytest.raises(ValueError, match="image mode"):
        settings.ImagesConfig(mode=mode)


@pytest.mark.parametrize("mode", ["passthrough", "local", "s3"])
def test_the_three_image_modes_are_accepted(mode):
    assert settings.ImagesConfig(mode=mode).mode == mode


def _scrape(root, source, stamp, marker):
    folder = root / source / stamp
    folder.mkdir(parents=True)
    (folder / "products.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    if marker is not None:
        (folder / "scrape_complete.json").write_text(marker, encoding="utf-8")
    return folder / "products.csv"


def test_a_corrupt_completion_marker_aborts_instead_of_reading_as_healthy(tmp_path):
    products = _scrape(tmp_path, "src", "20260101_000000", "{not json")
    with pytest.raises(SystemExit, match="scrape_complete.json"):
        run_mod.find_scrape_file("src", tmp_path)
    with pytest.raises(SystemExit, match="scrape_complete.json"):
        run_mod._scrape_detail_failure_ratio(products)


def test_marker_semantics_are_unchanged_for_valid_and_absent_markers(tmp_path):
    complete = _scrape(tmp_path, "src", "20260102_000000", json.dumps({"complete": True, "detail_failure_ratio": 0.25}))
    _scrape(tmp_path, "src", "20260103_000000", json.dumps({"complete": False}))   # newest, truncated: skipped
    legacy = _scrape(tmp_path, "old", "20260101_000000", None)
    assert run_mod.find_scrape_file("src", tmp_path) == complete
    assert run_mod._scrape_detail_failure_ratio(complete) == 0.25
    assert run_mod.find_scrape_file("old", tmp_path) == legacy
    assert run_mod._scrape_detail_failure_ratio(legacy) == 0.0
    assert run_mod.find_scrape_file("never", tmp_path) is None
