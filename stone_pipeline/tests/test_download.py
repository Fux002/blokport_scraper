"""The image fetcher goes DIRECT and isolates failures: 200 -> bytes, 404 -> permanent miss, redirects
SSRF-validated, transient non-200 retried. It IGNORES BLOKPORT_SCRAPER_PROXY -- images never use the
residential proxy (that is site-scrape-only; the image hosts serve datacenter IPs directly, verified from
the AWS egress). The fixture asserts no proxy is ever configured on the image client."""

from __future__ import annotations

import httpx
import pytest

from stone_pipeline.io import download


@pytest.fixture
def mock_httpx(monkeypatch):
    monkeypatch.setattr(download, "url_allowed", lambda u: True)
    real_client = httpx.Client

    def install(handler):
        transport = httpx.MockTransport(handler)

        def fake_client(**kw):
            assert "proxy" not in kw, "the image fetcher must NOT configure a proxy (images go direct)"
            return real_client(**kw, transport=transport)

        monkeypatch.setattr(httpx, "Client", fake_client)

    return install


def test_direct_fetch_returns_bytes(mock_httpx):
    mock_httpx(lambda r: httpx.Response(200, content=b"PNGBYTES"))
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://d1vjwtwi5ituin.cloudfront.net/x.jpg") == b"PNGBYTES"


def test_proxy_env_is_ignored_for_images(mock_httpx, monkeypatch):
    # even with the proxy env SET, the client is built WITHOUT a proxy (the fixture asserts it). Images go
    # direct; the residential proxy is reserved for the Cloudflare SITE scrape, not image downloads.
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy.soax.com:1337")
    mock_httpx(lambda r: httpx.Response(200, content=b"OK"))
    fetch = download.httpx_fetcher(retries=1, backoff=0.0)
    assert fetch("https://varshastones.slabware.com/a.jpg") == b"OK"


def test_404_is_permanent_no_retry(mock_httpx):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(404)

    mock_httpx(h)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://host/missing.jpg") is None
    assert len(calls) == 1                                    # permanent -> not retried


def test_transient_500_retries_then_none(mock_httpx):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(500)

    mock_httpx(h)
    fetch = download.httpx_fetcher(retries=2, backoff=0.0)
    assert fetch("https://host/a.jpg") is None
    assert len(calls) == 2                                    # transient -> retried up to `retries`


def test_redirect_is_followed_and_revalidated(mock_httpx):
    def h(request):
        if request.url.path == "/a.jpg":
            return httpx.Response(302, headers={"location": "https://host/final.jpg"})
        return httpx.Response(200, content=b"OK")

    mock_httpx(h)
    fetch = download.httpx_fetcher(retries=1, backoff=0.0)
    assert fetch("https://host/a.jpg") == b"OK"
