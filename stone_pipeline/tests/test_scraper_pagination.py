"""GAP A: pagination end-detection must not mistake a valid-but-empty mid-pagination response for the clean
end of the list (which is marked COMPLETE and silently delists the missing tail). marenostone uses the
WooCommerce X-WP-TotalPages header; the SlabWare scrapers (varsha/polonine), which expose no total, confirm
an empty batch with one re-fetch of the same offset before accepting the end. These are the regression guards
(zucchi already uses its own total; temmer already detects end-of-pages via a 404)."""

from __future__ import annotations

from scrapers.marenostone import MarenoStoneScraper
from scrapers.polonine import PolonineScraper
from scrapers.varsha import VarshaScraper


class _Resp:
    """Minimal WooCommerce Store API response: .json() items + the X-WP-TotalPages header (when advertised)."""

    def __init__(self, items, total_pages: int = 0):
        self._items = items
        self.headers = {"X-WP-TotalPages": str(total_pages)} if total_pages else {}

    def json(self):
        return self._items


def _seq_fetch(batches):
    """A _fetch_page stub that returns each queued batch in call order (regardless of offset), so an empty
    batch followed by a non-empty one exercises the transient-recovery re-fetch."""
    it = iter(batches)
    return lambda inicio: next(it)


# -- marenostone: WooCommerce total-pages header ------------------------------------------------------------

def test_marenostone_flags_an_empty_page_before_the_advertised_end(tmp_path, monkeypatch):
    s = MarenoStoneScraper(data_dir=tmp_path)
    pages = {1: _Resp([{"id": 1}], total_pages=3), 2: _Resp([], total_pages=3)}   # empty at page 2 of 3
    monkeypatch.setattr(s, "get", lambda url, params=None: pages[params["page"]])
    got = list(s.list_products())
    assert got == [{"id": 1}]
    assert s._incomplete is True                 # truncation flagged, NOT a clean end -> no silent delist


def test_marenostone_clean_end_at_the_advertised_total(tmp_path, monkeypatch):
    s = MarenoStoneScraper(data_dir=tmp_path)
    pages = {1: _Resp([{"i": 1}] * 100, total_pages=2), 2: _Resp([{"i": 2}] * 40, total_pages=2)}
    monkeypatch.setattr(s, "get", lambda url, params=None: pages[params["page"]])
    got = list(s.list_products())
    assert len(got) == 140 and s._incomplete is False


def test_marenostone_without_a_header_falls_back_to_the_short_page_signal(tmp_path, monkeypatch):
    s = MarenoStoneScraper(data_dir=tmp_path)
    pages = {1: _Resp([{"i": 1}] * 100), 2: _Resp([{"i": 2}] * 10)}   # no header -> short page ends it
    monkeypatch.setattr(s, "get", lambda url, params=None: pages[params["page"]])
    got = list(s.list_products())
    assert len(got) == 110 and s._incomplete is False


# -- SlabWare (varsha / polonine): confirm an empty batch with a re-fetch -----------------------------------

def test_varsha_recovers_a_transient_empty_and_confirms_the_real_end(tmp_path, monkeypatch):
    s = VarshaScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_warm_up", lambda: None)
    # first=[a]; [b]; [] (transient) -> re-fetch [c]; [] -> re-fetch [] (real end)
    monkeypatch.setattr(s, "_fetch_page", _seq_fetch([[{"a": 1}], [{"b": 1}], [], [{"c": 1}], [], []]))
    got = list(s.list_products())
    assert got == [{"a": 1}, {"b": 1}, {"c": 1}]   # transient empty did NOT truncate the tail
    assert s._incomplete is False                   # a confirmed empty is a clean end, not a failure


def test_varsha_stops_on_a_confirmed_empty(tmp_path, monkeypatch):
    s = VarshaScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_warm_up", lambda: None)
    monkeypatch.setattr(s, "_fetch_page", _seq_fetch([[{"a": 1}], [], []]))   # first=[a]; [] then confirm []
    got = list(s.list_products())
    assert got == [{"a": 1}] and s._incomplete is False


def test_polonine_recovers_a_transient_empty_before_ending(tmp_path, monkeypatch):
    s = PolonineScraper(data_dir=tmp_path)
    monkeypatch.setattr(s, "_warm_up", lambda: None)
    monkeypatch.setattr(s, "_fetch_page", _seq_fetch([[{"a": 1}], [], [{"c": 1}], [], []]))
    got = list(s.list_products())
    assert got == [{"a": 1}, {"c": 1}] and s._incomplete is False
