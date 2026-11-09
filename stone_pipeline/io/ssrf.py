"""SSRF guard for outbound fetches.

Image URLs (and links) come from scraped HTML, which is only semi-trusted: a malicious
or compromised source could point one -- or a redirect -- at an internal/link-local
address (e.g. the Fargate task-metadata IP 169.254.170.2) to read credentials. This
restricts fetches to http(s) on hosts that resolve EXCLUSIVELY to public addresses, and
callers apply it to every redirect hop (so a public URL can't 302 to an internal one).

Note: a small TOCTOU window (DNS rebinding) remains between the resolve-and-check here
and the client's own connect-time resolve. For the threat model -- scraped image URLs,
not a determined rebinding attacker -- blocking on the resolved IP plus per-hop redirect
validation is a strong mitigation. Pinning the socket to the resolved IP would close it
fully but needs a custom transport; revisit if the threat model hardens.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Protocol, TypeVar
from urllib.parse import urlparse

import httpx

from stone_pipeline.core import logfmt

log = logfmt.get_logger("ssrf")

_ALLOWED_SCHEMES = ("http", "https")
MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class SSRFBlocked(Exception):
    """A hop's URL is not public http(s). Permanent: a caller never retries it."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


class RedirectFailed(Exception):
    """A redirect with no Location, or more hops than MAX_REDIRECTS."""


class _Answer(Protocol):
    status_code: int
    headers: object


Answer = TypeVar("Answer", bound=_Answer)


def _ip_is_public(ip: str) -> bool:
    """Globally reachable by IANA's special-purpose registries (so not private, loopback, link-local, reserved,
    unspecified or the 100.64.0.0/10 shared CGNAT space), and not multicast, which is_global does not exclude."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_global and not addr.is_multicast


def url_allowed(url: str) -> bool:
    """True only for an http(s) URL whose host resolves exclusively to public IPs.
    Anything unresolvable, non-http(s), or resolving to a private/loopback/link-local/
    reserved/multicast address is rejected."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
        return False
    if "@" in parsed.netloc:        # userinfo: a redirect must never carry credentials to the next host
        return False
    try:
        ips = [info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)]
    except Exception:
        return False  # unresolvable -> block
    return bool(ips) and all(_ip_is_public(ip) for ip in ips)


def follow_redirects(url: str, send: Callable[[str], Answer]) -> tuple[str, Answer]:
    """Request `url` through `send`, following redirects by hand so every hop is checked by url_allowed BEFORE it is
    requested: a public URL must not redirect into an internal one. `send(hop)` makes one request that does not follow
    redirects itself. A relative Location resolves against the hop that gave it. Returns the final URL and its answer
    (any status but a redirect). Only status_code and headers are read, so it serves any HTTP client; a caller that
    streams releases a redirect's answer in its own `send`."""
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not url_allowed(current):
            raise SSRFBlocked(current)
        answer = send(current)
        if answer.status_code not in REDIRECT_STATUSES:
            return current, answer
        location = answer.headers.get("location")
        if not location:
            raise RedirectFailed(f"HTTP {answer.status_code} redirect without a Location: {current}")
        current = str(httpx.URL(current).join(location))
    raise RedirectFailed(f"too many redirects: {url}")
