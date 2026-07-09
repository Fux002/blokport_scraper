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

    SSRF guard: redirects are followed MANUALLY so every hop is validated by url_allowed (a public URL
    must not 302 into an internal/link-local one).

    NO proxy: image downloads go DIRECT. The residential proxy (BLOKPORT_SCRAPER_PROXY) exists for the
    SITE scrape of Cloudflare-fronted sources (scrapers/base.py, use_curl_cffi), whose dynamic pages block
    datacenter IPs. The IMAGE hosts do not: CloudFront (zucchi), LiteSpeed (marenostone), Google Drive
    (develi), and even the Cloudflare-fronted slabware.com STATIC images all serve the AWS/ECS datacenter
    egress directly (verified 200 from that egress). Routing images through the metered, per-GB residential
    proxy was both unnecessary and the cause of a large proxy-bandwidth burn, so the fetcher never uses it.
    A source whose images ever DO need a proxy is a per-source config decision, not a default here."""
    import httpx

    client = httpx.Client(timeout=timeout, follow_redirects=False,
                          limits=httpx.Limits(max_connections=16, max_keepalive_connections=8))

    def fetch(url: str) -> Optional[bytes]:
        for attempt in range(retries):
            try:
                current = url
                for _ in range(_MAX_REDIRECTS + 1):
                    if not url_allowed(current):
                        log.warning("blocked by SSRF guard (non-public host or scheme)",
                                    extra={"extra_fields": {"url": current}})
                        return None
                    with client.stream("GET", current) as response:
                        if response.is_redirect:
                            loc = response.headers.get("location")
                            if not loc:
                                return None
                            current = str(httpx.URL(current).join(loc))  # resolve relative -> revalidate
                            continue
                        if response.status_code in (404, 410):
                            return None  # permanent, do not retry
                        if response.status_code != 200:
                            break  # other status -> retry with backoff
                        clen = response.headers.get("content-length")
                        if clen and clen.isdigit() and int(clen) > _MAX_BYTES:
                            log.warning("image exceeds size cap (declared)",
                                        extra={"extra_fields": {"url": current, "bytes": clen}})
                            return None
                        buf = bytearray()
                        for chunk in response.iter_bytes():
                            buf += chunk
                            if len(buf) > _MAX_BYTES:
                                log.warning("image exceeds size cap (streamed)",
                                            extra={"extra_fields": {"url": current}})
                                return None
                        return bytes(buf) if buf else None
                else:
                    log.warning("too many redirects", extra={"extra_fields": {"url": url}})
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
