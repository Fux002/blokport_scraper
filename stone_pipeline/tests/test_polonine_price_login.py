"""Polonine's customer login, which shows its prices (the owner's own account, 14 Nov): both values from the
environment, never in code; a failed login never stops the product and stock scrape, which needs none, and the scrape
states in its completion marker whether it read prices. The secret is never written anywhere."""

from __future__ import annotations

import json

import pytest

from scrapers.polonine import LIST_API, LOGIN_API, LOGOUT_API, PolonineScraper

USER = "buyer@example.com"
SECRET = "s3cr'et\"pw"


class _R:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _scraper(tmp_path, monkeypatch, answer="ok", raises=None):
    scraper = PolonineScraper(data_dir=tmp_path)
    posts: list[tuple[str, str]] = []
    batches = [[{"id": 1}], []]

    def post(url, headers=None, content=None):
        posts.append((url, content))
        if url == LOGIN_API:
            if raises is not None:
                raise raises
            return _R({"d": answer})
        if url == LIST_API:
            return _R({"d": {"Bundles": json.dumps(batches.pop(0) if batches else [])}})
        if url == LOGOUT_API:
            return _R({"d": None})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(scraper, "post", post)
    monkeypatch.setattr(scraper, "get", lambda url, **kwargs: None)
    return scraper, posts


@pytest.fixture
def login_env(monkeypatch):
    monkeypatch.setenv("SCRAPER_POLONINE_USERNAME", USER)
    monkeypatch.setenv("SCRAPER_POLONINE_PASSWORD", SECRET)


@pytest.fixture
def no_login_env(monkeypatch):
    for name in ("SCRAPER_POLONINE_USERNAME", "SCRAPER_POLONINE_PASSWORD",
                 "BLOKPORT_POLONINE_USERNAME", "BLOKPORT_POLONINE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_a_login_logs_in_before_the_list_and_out_after_it(tmp_path, monkeypatch, login_env):
    scraper, posts = _scraper(tmp_path, monkeypatch)
    assert list(scraper.list_products()) == [{"id": 1}]
    assert scraper.run_facts["prices"] == "ok"
    urls = [url for url, _ in posts]
    assert urls[0] == LOGIN_API and urls[-1] == LOGOUT_API and LIST_API in urls
    # sent as the site's own login form sends it (slabware.js strips quotes from both values)
    assert json.loads(posts[0][1]) == {"login": USER, "senha": "s3cretpw"}


def test_no_login_in_the_environment_scrapes_products_without_prices_and_says_so(tmp_path, monkeypatch,
                                                                               no_login_env):
    scraper, posts = _scraper(tmp_path, monkeypatch)
    assert list(scraper.list_products()) == [{"id": 1}]
    assert "SCRAPER_POLONINE_USERNAME" in scraper.run_facts["prices"]
    assert LOGIN_API not in [url for url, _ in posts]
    assert any(failure["kind"] == "price_login" for failure in scraper._failures)


def test_a_refused_login_never_stops_the_product_scrape(tmp_path, monkeypatch, login_env):
    scraper, posts = _scraper(tmp_path, monkeypatch, answer="Invalid user or password")
    assert list(scraper.list_products()) == [{"id": 1}]
    assert scraper.run_facts["prices"] == "login refused: Invalid user or password"
    assert LOGOUT_API not in [url for url, _ in posts]


def test_a_login_that_gets_no_answer_never_stops_the_product_scrape(tmp_path, monkeypatch, login_env):
    scraper, _ = _scraper(tmp_path, monkeypatch, raises=RuntimeError("POST failed after 4 tries"))
    assert list(scraper.list_products()) == [{"id": 1}]
    assert scraper.run_facts["prices"] == "login request failed: RuntimeError"


@pytest.mark.parametrize("answer", ["ok", "Invalid user or password"])
def test_the_secret_is_never_written(tmp_path, monkeypatch, login_env, answer):
    scraper, _ = _scraper(tmp_path, monkeypatch, answer=answer)
    list(scraper.list_products())
    written = json.dumps([scraper.run_facts, scraper._failures], default=str)
    assert SECRET not in written and "s3cretpw" not in written


def test_the_completion_marker_carries_what_the_scraper_states_about_its_run(tmp_path, monkeypatch, login_env):
    scraper, _ = _scraper(tmp_path, monkeypatch)
    monkeypatch.setattr(scraper, "parse_product", lambda raw: {"product_id": str(raw["id"]), "image_urls": []})
    scraper.run()
    marker = json.loads(scraper.complete_marker.read_text(encoding="utf-8"))
    assert marker["prices"] == "ok" and marker["rows"] == 1
