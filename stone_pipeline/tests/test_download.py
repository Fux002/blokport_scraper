"""The image fetcher's proxy handling. A residential proxy is used when set, but a PUBLIC CDN the proxy
refuses with 407 (zucchi on CloudFront, develi on Google Drive) is fetched DIRECT instead of failing --
the proxy refuses the target, not the target refusing us. Plus the settled cases (200, 404)."""

from __future__ import annotations

import httpx
import pytest

from stone_pipeline.io import download


@pytest.fixture
def mock_httpx(monkeypatch):
    """Route every httpx.Client (proxied AND direct) through an injectable MockTransport, and let every
    target pass the SSRF guard, so the fetcher's routing logic is tested without network or DNS."""
    monkeypatch.setattr(download, "url_allowed", lambda u: True)
    real_client = httpx.Client

    def install(handler):
        transport = httpx.MockTransport(handler)

        def fake_client(**kw):
            kw.pop("proxy", None)  # the mock transport serves regardless of the proxy setting
            return real_client(**kw, transport=transport)

        monkeypatch.setattr(httpx, "Client", fake_client)

    return install


def test_proxy_407_falls_back_to_direct(mock_httpx, monkeypatch):
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:8080")
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:                                  # first hop = via the proxy -> refused
            return httpx.Response(407, text="Proxy Authentication Required")
        return httpx.Response(200, content=b"PNGBYTES")       # direct -> served

    mock_httpx(handler)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://d1vjwtwi5ituin.cloudfront.net/x.jpg") == b"PNGBYTES"
    assert len(calls) == 2                                    # proxy (407) then direct (200)


def test_proxy_200_does_not_fall_back(mock_httpx, monkeypatch):
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:8080")
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"OK")

    mock_httpx(handler)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://cloudflare.example/a.jpg") == b"OK"
    assert len(calls) == 1                                    # proxy served it: no needless direct hop


def test_no_proxy_fetches_direct(mock_httpx, monkeypatch):
    monkeypatch.delenv("BLOKPORT_SCRAPER_PROXY", raising=False)
    mock_httpx(lambda request: httpx.Response(200, content=b"OK"))
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://host/a.jpg") == b"OK"


def test_404_is_a_permanent_miss_no_retry(mock_httpx, monkeypatch):
    monkeypatch.delenv("BLOKPORT_SCRAPER_PROXY", raising=False)
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    mock_httpx(handler)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://host/missing.jpg") is None
    assert len(calls) == 1                                    # 404 is permanent -> not retried
