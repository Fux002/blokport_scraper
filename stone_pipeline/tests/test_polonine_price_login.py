"""Polonine's prices, shown only to a logged-in customer (the owner's own account, 14 Nov): read by a SEPARATE price
pass after the anonymous product and stock scrape, which stays exactly as it was (the manager's ruling 14 Nov: the
product set never depends on the login). The price pass writes only its own files (prices.csv and its marker), takes
both credentials from the environment and never writes them, and a missing, refused or failing login leaves the
products identical. New names are imported inside the tests, so each spec fails for its own reason."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scrapers.polonine import DETAIL_API, LIST_API, PolonineScraper

USER = "buyer@example.com"
SECRET = "s3cr'et\"pw"
BUNDLE = {"id": 1, "nomeMaterial": "ALPINE", "nomeComposicao": "Dolomite Marble", "nomeEspessura": "2CM",
          "acabamento": "Polished", "nomeQualidade": "A", "bloco": "B7", "cavalete": "C1", "averageSize": ""}
PRICED = {"preco1": "US$ 59.00<span>/Sq.Mt.</span>", "preco2": "US$ 5.48<span>/Sq.Ft.</span>"}


def _api():
    from scrapers import polonine
    return polonine.LOGIN_API, polonine.LOGOUT_API


class _R:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Html:
    """An answer that is a page, not JSON (a session page or a redirect target after the login)."""

    def json(self):
        return json.loads("<html><body>Session expired</body></html>")


class _Site:
    """Polonine's SlabWare API: the list carries prices only to a logged-in session."""

    def __init__(self, answer="ok", login_raises=None, list_raises_logged_in=None, list_html_logged_in=False):
        self.answer, self.login_raises, self.list_raises_logged_in = answer, login_raises, list_raises_logged_in
        self.list_html_logged_in = list_html_logged_in
        self.logged_in = False
        self.posts: list[tuple[str, str, bool]] = []

    def post(self, url, headers=None, content=None):
        login_api, logout_api = _api()
        self.posts.append((url, content, self.logged_in))
        if url == login_api:
            if self.login_raises is not None:
                raise self.login_raises
            self.logged_in = self.answer == "ok"
            # a list answer is the whole JSON body (not an object), as a misbehaving endpoint could send
            return _R(self.answer if isinstance(self.answer, list) else {"d": self.answer})
        if url == logout_api:
            self.logged_in = False
            return _R({"d": None})
        if url == LIST_API:
            if self.logged_in and self.list_raises_logged_in is not None:
                raise self.list_raises_logged_in
            if self.logged_in and self.list_html_logged_in:
                return _Html()
            offset = json.loads(content)["inicio"]
            item = {**BUNDLE, **(PRICED if self.logged_in else {"preco1": "", "preco2": ""})}
            return _R({"d": {"Bundles": json.dumps([item] if offset == 0 else [])}})
        if url == DETAIL_API:
            return _R({"d": {"Bundle": {"fotos": ["/a.jpg"], "chapas": [], "totalSqmt": "55.160"}}})
        raise AssertionError(f"unexpected POST {url}")


def _scraper(root: Path, monkeypatch, site: _Site) -> PolonineScraper:
    scraper = PolonineScraper(data_dir=root)
    monkeypatch.setattr(scraper, "post", site.post)
    monkeypatch.setattr(scraper, "get", lambda url, **kwargs: None)
    return scraper


@pytest.fixture
def login_env(monkeypatch):
    monkeypatch.setenv("SCRAPER_POLONINE_USERNAME", USER)
    monkeypatch.setenv("SCRAPER_POLONINE_PASSWORD", SECRET)


@pytest.fixture
def no_login_env(monkeypatch):
    for name in ("SCRAPER_POLONINE_USERNAME", "SCRAPER_POLONINE_PASSWORD",
                 "BLOKPORT_POLONINE_USERNAME", "BLOKPORT_POLONINE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def _products(scraper: PolonineScraper) -> list[dict]:
    """products.csv without the per-row clock (scrape_timestamp), which differs between any two runs."""
    with scraper.products_csv.open(encoding="utf-8", newline="") as handle:
        return [{k: v for k, v in row.items() if k != "scrape_timestamp"} for row in csv.DictReader(handle)]


def _price_marker(scraper: PolonineScraper) -> dict:
    return json.loads((scraper.run_dir / "prices_complete.json").read_text(encoding="utf-8"))


def test_the_product_pass_never_logs_in(tmp_path, monkeypatch, login_env):
    site = _Site()
    scraper = _scraper(tmp_path, monkeypatch, site)
    assert [item["id"] for item in scraper.list_products()] == [1]
    assert _api()[0] not in [url for url, _, _ in site.posts]
    assert not any(logged_in for _, _, logged_in in site.posts)


def test_login_on_or_off_gives_an_identical_products_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRAPER_POLONINE_USERNAME", USER)
    monkeypatch.setenv("SCRAPER_POLONINE_PASSWORD", SECRET)
    with_login = _scraper(tmp_path / "on", monkeypatch, _Site())
    with_login.run()
    monkeypatch.delenv("SCRAPER_POLONINE_USERNAME")
    without = _scraper(tmp_path / "off", monkeypatch, _Site())
    without.run()
    assert _products(with_login) == _products(without)
    assert _products(with_login)[0]["price1"] == ""                  # the product pass is the anonymous one
    assert json.loads(with_login.complete_marker.read_text()) == json.loads(without.complete_marker.read_text()) | {
        "timestamp": json.loads(with_login.complete_marker.read_text())["timestamp"]}


def test_the_price_pass_writes_its_own_rows_and_marker_and_logs_out(tmp_path, monkeypatch, login_env):
    site = _Site()
    scraper = _scraper(tmp_path, monkeypatch, site)
    scraper.run()
    assert _price_marker(scraper) == {"prices": "ok", "rows": 1}
    with (scraper.run_dir / "prices.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert (rows[0]["product_id"], rows[0]["price1"], rows[0]["material"]) == ("1", PRICED["preco1"], "ALPINE")
    login_api, logout_api = _api()
    urls = [url for url, _, _ in site.posts]
    assert urls.index(login_api) > urls.index(DETAIL_API) and urls[-1] == logout_api
    # sent as the site's own login form sends it (slabware.js strips quotes from both values)
    assert json.loads(next(body for url, body, _ in site.posts if url == login_api)) == {"login": USER,
                                                                                         "senha": "s3cretpw"}


def test_no_login_in_the_environment_reads_no_prices_and_says_so(tmp_path, monkeypatch, no_login_env):
    scraper = _scraper(tmp_path, monkeypatch, _Site())
    scraper.run()
    assert "SCRAPER_POLONINE_USERNAME" in _price_marker(scraper)["prices"]
    assert not (scraper.run_dir / "prices.csv").exists()
    assert json.loads(scraper.complete_marker.read_text())["complete"] is True      # the products are untouched


def test_a_refused_login_is_stated_without_the_sites_words(tmp_path, monkeypatch, login_env):
    scraper = _scraper(tmp_path, monkeypatch, _Site(answer=f"Unknown user {USER}"))
    scraper.run()
    assert _price_marker(scraper)["prices"] == f"login refused ({len('Unknown user ' + USER)} characters)"
    assert not (scraper.run_dir / "prices.csv").exists()


def test_a_login_with_no_answer_reads_no_prices(tmp_path, monkeypatch, login_env):
    scraper = _scraper(tmp_path, monkeypatch, _Site(login_raises=RuntimeError("POST failed after 4 tries")))
    scraper.run()
    assert _price_marker(scraper)["prices"] == "login request failed: RuntimeError"


def test_a_price_list_that_fails_still_logs_out_and_reads_no_prices(tmp_path, monkeypatch, login_env):
    site = _Site(list_raises_logged_in=RuntimeError("POST failed after 4 tries"))
    scraper = _scraper(tmp_path, monkeypatch, site)
    scraper.run()
    assert _price_marker(scraper) == {"prices": "price list failed: RuntimeError", "rows": 0}
    assert site.posts[-1][0] == _api()[1]
    assert json.loads(scraper.complete_marker.read_text())["complete"] is True


@pytest.mark.parametrize("site", [_Site(), _Site(answer=f"Unknown user {USER}")])
def test_neither_the_secret_nor_the_username_is_ever_written(tmp_path, monkeypatch, login_env, site):
    scraper = _scraper(tmp_path, monkeypatch, site)
    scraper.run()
    written = "".join(path.read_text(encoding="utf-8") for path in scraper.run_dir.rglob("*") if path.is_file())
    assert SECRET not in written and "s3cretpw" not in written and USER not in written


# --- the price pass can never abort the product scrape (f8's must, 14 Nov) -------------------------------------------
def _identical_products_beside(tmp_path, monkeypatch, site, prepare=None):
    """Run once with the login and the given site, once without any login, and return both scrapers."""
    monkeypatch.setenv("SCRAPER_POLONINE_USERNAME", USER)
    monkeypatch.setenv("SCRAPER_POLONINE_PASSWORD", SECRET)
    with_login = _scraper(tmp_path / "on", monkeypatch, site)
    if prepare is not None:
        prepare(with_login)
    assert with_login.run() == with_login.products_csv
    monkeypatch.delenv("SCRAPER_POLONINE_USERNAME")
    without = _scraper(tmp_path / "off", monkeypatch, _Site())
    without.run()
    assert _products(with_login) == _products(without)
    assert json.loads(with_login.complete_marker.read_text())["complete"] is True
    return with_login


def test_a_logged_in_list_that_answers_a_page_reads_no_prices_and_leaves_the_products(tmp_path, monkeypatch):
    site = _Site(list_html_logged_in=True)
    scraper = _identical_products_beside(tmp_path, monkeypatch, site)
    assert _price_marker(scraper) == {"prices": "price list failed: JSONDecodeError", "rows": 0}
    assert site.posts[-1][0] == _api()[1]                       # logged out all the same


def test_a_login_answer_that_is_not_an_object_reads_no_prices_and_leaves_the_products(tmp_path, monkeypatch):
    scraper = _identical_products_beside(tmp_path, monkeypatch, _Site(answer=["ok"]))
    assert _price_marker(scraper)["prices"] == "login request failed: AttributeError"


def test_price_files_that_cannot_be_written_never_fail_the_product_scrape(tmp_path, monkeypatch):
    def unwritable(scraper):
        def refuse(rows, columns, prices):
            raise OSError("disk full")
        monkeypatch.setattr(scraper, "write_price_pass", refuse)
    scraper = _identical_products_beside(tmp_path, monkeypatch, _Site(), unwritable)
    assert not (scraper.run_dir / "prices_complete.json").exists()
