"""The image fetcher's proxy handling. A residential proxy is used when set, but when the proxy REFUSES a
target -- an https CONNECT 407 raised as httpx.ProxyError (the common case), or a rare http 407 response --
the fetcher retries the SAME url DIRECT, so a public CDN (zucchi/CloudFront, marenostone, develi) still
downloads even when the proxy will not serve it or is down. The fixture gives the proxied and direct
clients DISTINCT transports, so a test that 'passes' truly proves the switch to direct happened."""

from __future__ import annotations

import httpx
import pytest

from stone_pipeline.io import download


@pytest.fixture
def mock_httpx(monkeypatch):
    """Install httpx.Client so the PROXIED client (created with proxy=...) and the DIRECT client get
    different MockTransports, and every target passes the SSRF guard. So we can assert exactly which
    client served the request."""
    monkeypatch.setattr(download, "url_allowed", lambda u: True)
    real_client = httpx.Client

    def install(proxied_handler, direct_handler):
        def fake_client(**kw):
            is_proxied = kw.pop("proxy", None) is not None
            transport = httpx.MockTransport(proxied_handler if is_proxied else direct_handler)
            return real_client(**kw, transport=transport)

        monkeypatch.setattr(httpx, "Client", fake_client)

    return install


def test_proxy_error_exception_falls_back_to_direct(mock_httpx, monkeypatch):
    # THE COMMON CASE: an https image; the proxy refuses the CONNECT tunnel, which httpx raises as
    # httpx.ProxyError (an EXCEPTION, not a response). The proxy ALWAYS refuses; only a switch to the
    # direct client can succeed -- so this genuinely proves the fallback.
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:8080")
    proxied, direct = [], []

    def proxied_handler(request):
        proxied.append(str(request.url))
        raise httpx.ProxyError("407 Proxy Authentication Required")

    def direct_handler(request):
        direct.append(str(request.url))
        return httpx.Response(200, content=b"PNGBYTES")

    mock_httpx(proxied_handler, direct_handler)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://d1vjwtwi5ituin.cloudfront.net/x.jpg") == b"PNGBYTES"
    assert len(proxied) == 1 and len(direct) == 1            # proxy tried once, then direct served


def test_proxy_407_response_falls_back_to_direct(mock_httpx, monkeypatch):
    # the rarer http case: the proxy refuses with a 407 RESPONSE (no CONNECT tunnel). Also -> direct.
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:8080")
    proxied, direct = [], []

    def proxied_handler(request):
        proxied.append(1)
        return httpx.Response(407, text="Proxy Authentication Required")

    def direct_handler(request):
        direct.append(1)
        return httpx.Response(200, content=b"PNGBYTES")

    mock_httpx(proxied_handler, direct_handler)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("http://cdn.example/x.jpg") == b"PNGBYTES"
    assert len(proxied) == 1 and len(direct) == 1


def test_proxy_success_never_touches_direct(mock_httpx, monkeypatch):
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:8080")
    proxied, direct = [], []

    def proxied_handler(request):
        proxied.append(1)
        return httpx.Response(200, content=b"OK")

    def direct_handler(request):
        direct.append(1)
        return httpx.Response(200, content=b"SHOULD-NOT-BE-USED")

    mock_httpx(proxied_handler, direct_handler)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://cloudflare.example/a.jpg") == b"OK"
    assert len(proxied) == 1 and direct == []               # proxy served it: no needless direct hop


def test_host_reachable_only_via_dead_proxy_fails_loud(mock_httpx, monkeypatch):
    # a Cloudflare-fronted host that the direct client cannot reach either -> None, not a hang/crash.
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy:8080")

    def refuse(request):
        raise httpx.ProxyError("407 Proxy Authentication Required")

    def blocked(request):
        raise httpx.ConnectError("blocked")

    mock_httpx(refuse, blocked)
    fetch = download.httpx_fetcher(retries=2, backoff=0.0)
    assert fetch("https://slabware.example/a.jpg") is None


def test_no_proxy_fetches_direct(mock_httpx, monkeypatch):
    monkeypatch.delenv("BLOKPORT_SCRAPER_PROXY", raising=False)
    mock_httpx(lambda r: httpx.Response(500), lambda r: httpx.Response(200, content=b"OK"))
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://host/a.jpg") == b"OK"              # no proxy -> the single client is direct


def test_404_is_a_permanent_miss_no_retry(mock_httpx, monkeypatch):
    monkeypatch.delenv("BLOKPORT_SCRAPER_PROXY", raising=False)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404)

    mock_httpx(handler, handler)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://host/missing.jpg") is None
    assert len(calls) == 1                                    # 404 permanent -> not retried
