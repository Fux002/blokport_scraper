"""The live HTTP transport behind fetching.Transport: the only module of this package that holds an HTTP client.

Plain httpx under our own honest name (config.USER_AGENT): no browser disguise, no TLS impersonation, no proxy (the
environment's proxy settings are ignored) and no retry, because a refusal is reported, never worked around (fetching
handles a 429 or a challenge as a block). Redirects are followed by stone_pipeline.io.ssrf.follow_redirects, the
scraper's own rule, so every hop is checked as a public http(s) address before it is requested. A body over
config.MAX_RESPONSE_BYTES is refused, before it is read when the site declares its size, else as it streams in.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import httpx

from market_prices import config
from market_prices.fetching import Response, TransportError, UnexpectedResponse
from stone_pipeline.io.ssrf import RedirectFailed, SSRFBlocked, follow_redirects


def _read_capped(answer: httpx.Response, url: str, deadline: float, clock: Callable[[], float]) -> bytes:
    """The body, under the size cap and the request's wall-clock deadline. httpx's timeout bounds each read, not the
    whole: a site sending a byte just inside it would otherwise hold the request until the size cap."""
    declared = answer.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > config.MAX_RESPONSE_BYTES:
        raise UnexpectedResponse(f"{url} declares {declared} bytes, over the cap of {config.MAX_RESPONSE_BYTES}")
    body = bytearray()
    for chunk in answer.iter_bytes():
        body += chunk
        if len(body) > config.MAX_RESPONSE_BYTES:
            raise UnexpectedResponse(f"{url} sent over {config.MAX_RESPONSE_BYTES} bytes")
        if clock() > deadline:
            raise UnexpectedResponse(f"{url} took over the {config.FETCH_DEADLINE_SECONDS}s deadline")
    return bytes(body)


class LiveTransport:
    """One client for one run. Use it as a context manager, so its connections are closed when the run ends.
    `http_transport` replaces the network for tests (httpx.MockTransport); production passes nothing."""

    def __init__(self, http_transport: Optional[httpx.BaseTransport] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._client = httpx.Client(timeout=config.FETCH_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False,
                                    headers={"User-Agent": config.USER_AGENT}, transport=http_transport)

    def __enter__(self) -> "LiveTransport":
        return self

    def __exit__(self, *exc_info) -> None:
        self._client.close()

    def get(self, url: str) -> Response:
        deadline = self._clock() + config.FETCH_DEADLINE_SECONDS
        opened: list[httpx.Response] = []

        def send(hop: str) -> httpx.Response:
            opened.append(self._client.send(self._client.build_request("GET", hop), stream=True))
            return opened[-1]

        try:
            try:
                final_url, answer = follow_redirects(url, send)
                body = _read_capped(answer, final_url, deadline, self._clock)
            finally:
                for each in opened:     # every hop, read or not, so no connection is left held
                    each.close()
        except SSRFBlocked as exc:
            where = (f"{url} is not a public http(s) address" if exc.url == url
                     else f"{url} redirects to {exc.url}, which is not a public http(s) address")
            raise UnexpectedResponse(where) from exc
        except (RedirectFailed, httpx.InvalidURL) as exc:
            raise UnexpectedResponse(f"{url}: {exc}") from exc
        except httpx.HTTPError as exc:     # timeout, connection, protocol or decoding failure: no usable answer
            raise TransportError(f"{url}: {type(exc).__name__}: {exc}") from exc
        return Response(status=answer.status_code, text=body.decode(answer.encoding or "utf-8", errors="replace"),
                        headers=dict(answer.headers), body=body)
