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
from urllib.parse import urlparse

from stone_pipeline.core import logfmt

log = logfmt.get_logger("ssrf")

_ALLOWED_SCHEMES = ("http", "https")


def _ip_is_public(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified)


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
    try:
        ips = [info[4][0] for info in socket.getaddrinfo(parsed.hostname, None)]
    except Exception:
        return False  # unresolvable -> block
    return bool(ips) and all(_ip_is_public(ip) for ip in ips)
