"""Bounded-concurrency image downloader (section 13A.2).

Every network call is fallible and isolated: a failed image returns None (the
caller flags the row and skips the slot), never a crashed run. Bounded
concurrency, per-host courtesy via a small connection pool, timeouts, and retry
with backoff on transient errors. Content-hash dedup happens in the image stage,
so an image is fetched once even across re-runs (the backend skips re-upload).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from stone_pipeline.core import logfmt
from stone_pipeline.io.ssrf import url_allowed

log = logfmt.get_logger("download")

_MAX_REDIRECTS = 5
# images come from semi-trusted scraped hosts; a body over this is not a catalog stone photo and is
# rejected so a hostile/broken host can't OOM the run (read in capped chunks, so a chunked response
# with no Content-Length is bounded too).
_MAX_BYTES = 25 * 1024 * 1024


def httpx_fetcher(timeout: float = 20.0, retries: int = 3, backoff: float = 0.5) -> Callable[[str], Optional[bytes]]:
    """Return a fetch(url) -> bytes | None using httpx, with retry and backoff.

    SSRF guard: redirects are followed MANUALLY so every hop is validated by
    url_allowed (a public URL must not 302 into an internal/link-local one) -- for BOTH the proxied and
    the direct client.

    Proxy: some supplier image hosts (Cloudflare-fronted) block datacenter IPs (the AWS/ECS NAT egress), so
    downloads route through the residential proxy when BLOKPORT_SCRAPER_PROXY is set. But other suppliers
    host on a PUBLIC CDN (zucchi on CloudFront, marenostone direct, develi on Google Drive) that datacenter
    IPs reach fine and that the proxy may REFUSE -- either the proxy provider rejects that target, or the
    proxy is simply down. A refusal is the PROXY failing, not the target, so we retry the SAME url DIRECT.
    For an HTTPS target (the usual case) the refusal is a 407 at the CONNECT tunnel, which httpx raises as
    httpx.ProxyError (an EXCEPTION, not a response); for a rare HTTP target it is a 407 response. Both switch
    to direct. One reactive lever, no per-host allowlist to drift: proxied hosts stay proxied, direct-
    reachable hosts fall through to direct, and a host reachable ONLY via a (dead) proxy still fails loud
    (None). Unset locally = the proxied client IS the direct client, so behaviour is unchanged there."""
    import os

    import httpx

    opts: dict = {"timeout": timeout, "follow_redirects": False,
                  "limits": httpx.Limits(max_connections=16, max_keepalive_connections=8)}
    proxy = os.environ.get("BLOKPORT_SCRAPER_PROXY", "").strip()
    direct = httpx.Client(**opts)
    proxied = httpx.Client(**opts, proxy=proxy) if proxy else direct
    if proxy:
        log.info("image fetcher routing through proxy (direct fallback on proxy 407)")

    def _attempt(client: "httpx.Client", url: str) -> tuple[Optional[bytes], Optional[int]]:
        """One fetch (following redirects) via `client`. Returns (data, status): (bytes|None, None) is a
        settled result -- image bytes, or None for a permanent miss (404/410/SSRF/too-big/redirect-loop);
        (None, int) is a retry-able non-200 the caller decides on (e.g. 407 -> switch to direct)."""
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            if not url_allowed(current):
                log.warning("blocked by SSRF guard (non-public host or scheme)",
                            extra={"extra_fields": {"url": current}})
                return None, None
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    loc = response.headers.get("location")
                    if not loc:
                        return None, None
                    current = str(httpx.URL(current).join(loc))  # resolve relative -> revalidate
                    continue
                if response.status_code in (404, 410):
                    return None, None  # permanent, do not retry
                if response.status_code != 200:
                    return None, response.status_code
                clen = response.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > _MAX_BYTES:
                    log.warning("image exceeds size cap (declared)",
                                extra={"extra_fields": {"url": current, "bytes": clen}})
                    return None, None
                buf = bytearray()
                for chunk in response.iter_bytes():
                    buf += chunk
                    if len(buf) > _MAX_BYTES:
                        log.warning("image exceeds size cap (streamed)",
                                    extra={"extra_fields": {"url": current}})
                        return None, None
                return (bytes(buf) if buf else None), None
        log.warning("too many redirects", extra={"extra_fields": {"url": url}})
        return None, None

    def _switch_to_direct(url: str, reason: str) -> "httpx.Client":
        """The proxy refused this target; log it once and hand back the direct client for an immediate
        retry. A single place so the 407-response and ProxyError-exception paths behave identically."""
        log.info("proxy refused target; fetching direct",
                 extra={"extra_fields": {"url": url, "reason": reason}})
        return direct

    def fetch(url: str) -> Optional[bytes]:
        client = proxied
        for attempt in range(retries):
            try:
                data, status = _attempt(client, url)
                if status is None:
                    return data  # settled: image bytes or a permanent miss
                if status == 407 and client is proxied and proxied is not direct:
                    # HTTP target: the proxy refused it with a 407 RESPONSE. Retry direct (below).
                    client = _switch_to_direct(url, "407 response")
                    continue
            except httpx.ProxyError as exc:
                # HTTPS target: a 407 (or any refusal) at the proxy CONNECT tunnel raises here as an
                # EXCEPTION, not a response -- this is the common path (image CDNs are https). The target is
                # often reachable directly (a public CDN, or the proxy is down), so switch and retry now.
                if client is proxied and proxied is not direct:
                    client = _switch_to_direct(url, str(exc))
                    continue
                if attempt == retries - 1:  # already direct / no proxy -> a real failure
                    log.warning("download failed", extra={"extra_fields": {"url": url, "error": str(exc)}})
                    return None
            except Exception as exc:  # transient: retry with backoff
                if attempt == retries - 1:
                    log.warning("download failed", extra={"extra_fields": {"url": url, "error": str(exc)}})
                    return None
            time.sleep(backoff * (2 ** attempt))
        return None

    return fetch


def fetch_many(urls: list[str], fetch: Callable[[str], Optional[bytes]], concurrency: int = 8) -> dict[str, Optional[bytes]]:
    """Fetch a set of distinct urls concurrently. Returns url -> bytes|None."""
    distinct = list(dict.fromkeys(u for u in urls if u))
    results: dict[str, Optional[bytes]] = {}
    if not distinct:
        return results
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for url, data in zip(distinct, pool.map(fetch, distinct)):
            results[url] = data
    return results
