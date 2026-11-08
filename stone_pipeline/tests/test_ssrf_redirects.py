"""The one SSRF-checked redirect walk (stone_pipeline.io.ssrf.follow_redirects) that the scraper and the market-price
transport both use: every hop is checked before it is requested, a relative Location is resolved against the hop it
came from, and the hops are capped."""

from __future__ import annotations

import pytest

from stone_pipeline.io import ssrf


class _Answer:
    def __init__(self, status: int, location: str | None = None):
        self.status_code = status
        self.headers = {"location": location} if location else {}


class _Hops:
    """Answers each requested URL from a script, recording the order the URLs were requested in."""

    def __init__(self, answers: dict[str, _Answer]):
        self.answers = answers
        self.sent: list[str] = []

    def __call__(self, url: str) -> _Answer:
        self.sent.append(url)
        return self.answers[url]


@pytest.fixture
def public(monkeypatch):
    """Every host is public except those named internal: no DNS in a test."""
    checked: list[str] = []

    def allowed(url: str) -> bool:
        checked.append(url)
        return "internal" not in url
    monkeypatch.setattr(ssrf, "url_allowed", allowed)
    return checked


def test_a_direct_answer_is_returned_with_its_url(public):
    hops = _Hops({"https://a.example/x": _Answer(200)})
    url, answer = ssrf.follow_redirects("https://a.example/x", hops)
    assert (url, answer.status_code, hops.sent) == ("https://a.example/x", 200, ["https://a.example/x"])


def test_every_hop_is_checked_and_a_relative_location_resolves_against_its_hop(public):
    hops = _Hops({"https://a.example/x": _Answer(301, "/y"), "https://a.example/y": _Answer(302, "https://b.example/z"),
                  "https://b.example/z": _Answer(200)})
    url, _ = ssrf.follow_redirects("https://a.example/x", hops)
    assert url == "https://b.example/z"
    assert public == hops.sent == ["https://a.example/x", "https://a.example/y", "https://b.example/z"]


def test_a_hop_to_a_non_public_host_is_refused_before_it_is_requested(public):
    hops = _Hops({"https://a.example/x": _Answer(302, "http://internal.example/meta")})
    with pytest.raises(ssrf.SSRFBlocked) as blocked:
        ssrf.follow_redirects("https://a.example/x", hops)
    assert blocked.value.url == "http://internal.example/meta"
    assert hops.sent == ["https://a.example/x"]


def test_a_redirect_without_a_location_fails(public):
    with pytest.raises(ssrf.RedirectFailed, match="redirect without a Location"):
        ssrf.follow_redirects("https://a.example/x", _Hops({"https://a.example/x": _Answer(302)}))


def test_the_hops_are_capped(public):
    hops = _Hops({"https://a.example/x": _Answer(301, "/x")})
    with pytest.raises(ssrf.RedirectFailed, match="too many redirects: https://a.example/x"):
        ssrf.follow_redirects("https://a.example/x", hops)
    assert len(hops.sent) == ssrf.MAX_REDIRECTS + 1
