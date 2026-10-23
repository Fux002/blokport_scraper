"""The image fetcher goes DIRECT and isolates failures: 200 -> bytes, 404 -> permanent miss, redirects
SSRF-validated, transient non-200 retried, a body that is not an image refused. It IGNORES
BLOKPORT_SCRAPER_PROXY -- images never use the residential proxy (that is site-scrape-only; the image hosts
serve datacenter IPs directly, verified from the AWS egress). The fixture asserts no proxy is ever
configured on the image client."""

from __future__ import annotations

from io import BytesIO

import httpx
import pytest
from PIL import Image

from stone_pipeline.io import download

# The exact body a supplier's nginx served for a WudPort photo (170 bytes), which reached the catalog as a
# product image labelled image/jpeg on 9 listings.
NGINX_500 = (b"<html>\r\n<head><title>500 Internal Server Error</title></head>\r\n<body>\r\n"
             b"<center><h1>500 Internal Server Error</h1></center>\r\n<hr><center>nginx</center>\r\n"
             b"</body>\r\n</html>\r\n")


def _image(fmt: str) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 3), (200, 190, 180)).save(buf, format=fmt)
    return buf.getvalue()


JPEG = _image("JPEG")


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
    mock_httpx(lambda r: httpx.Response(200, content=JPEG))
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://d1vjwtwi5ituin.cloudfront.net/x.jpg") == JPEG


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP"])
def test_an_image_is_accepted_whatever_type_the_host_declares(mock_httpx, fmt):
    data = _image(fmt)
    mock_httpx(lambda r: httpx.Response(200, content=data, headers={"content-type": "application/octet-stream"}))
    fetch = download.httpx_fetcher(retries=1, backoff=0.0)
    assert fetch("https://host/a.jpg") == data


@pytest.mark.parametrize("content_type", ["image/jpeg", "text/html"])
def test_an_error_page_served_as_200_is_refused_whatever_type_it_claims(mock_httpx, content_type):
    calls = []

    def h(r):
        calls.append(1)
        return httpx.Response(200, content=NGINX_500, headers={"content-type": content_type})

    mock_httpx(h)
    fetch = download.httpx_fetcher(retries=3, backoff=0.0)
    assert fetch("https://host/eucalyptus-back.jpg") is None
    assert len(calls) == 1                                    # a refused body is a verdict, not a transport retry


def test_a_non_image_body_is_refused_on_the_final_redirect_hop(mock_httpx):
    def h(request):
        if request.url.path == "/a.jpg":
            return httpx.Response(302, headers={"location": "https://host/interstitial"})
        return httpx.Response(200, content=b"<!DOCTYPE html><title>Virus scan warning</title>")

    mock_httpx(h)
    fetch = download.httpx_fetcher(retries=1, backoff=0.0)
    assert fetch("https://host/a.jpg") is None


def test_proxy_env_is_ignored_for_images(mock_httpx, monkeypatch):
    # even with the proxy env SET, the client is built WITHOUT a proxy (the fixture asserts it). Images go
    # direct; the residential proxy is reserved for the Cloudflare SITE scrape, not image downloads.
    monkeypatch.setenv("BLOKPORT_SCRAPER_PROXY", "http://u:p@proxy.soax.com:1337")
    mock_httpx(lambda r: httpx.Response(200, content=JPEG))
    fetch = download.httpx_fetcher(retries=1, backoff=0.0)
    assert fetch("https://varshastones.slabware.com/a.jpg") == JPEG


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
        return httpx.Response(200, content=JPEG)

    mock_httpx(h)
    fetch = download.httpx_fetcher(retries=1, backoff=0.0)
    assert fetch("https://host/a.jpg") == JPEG
