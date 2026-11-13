"""Turn hostile site text into one clean line, keeping the seller's words."""

from __future__ import annotations

import html
import math
import re
from typing import Optional
from urllib.parse import urlparse

# Must start like a real tag, so a seller's "Slab <3cm and >2cm" stays text.
_TAG = re.compile(r"<[/!]?[a-zA-Z-][^<>]*>")
_SPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_UNSAFE_IN_URL = re.compile(r"[\s\\@\x00-\x1f\x7f]")


def clean_text(value: object) -> Optional[str]:
    """Tags removed, entities decoded, whitespace collapsed; None when nothing is left. Tags go before entities
    are decoded, so an escaped "&lt;b&gt;" that the seller wrote as text survives as text."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = html.unescape(_TAG.sub(" ", str(value)))
    return _SPACE.sub(" ", _CONTROL.sub(" ", text)).strip() or None


def site_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def same_site(url: str, domain: str) -> bool:
    """Whether `url`'s host is `domain` or a subdomain of it (same_host)."""
    return same_host(site_host(url), domain)


def same_host(host: str, domain: str) -> bool:
    """Whether `host` is `domain` or a subdomain of it, with case and a leading "www." dropped on both sides. A whole
    host, never a registrable domain: two SlabWare tenants share slabware.com and are different sellers."""
    seen, wanted = (value.strip().lower().removeprefix("www.") for value in (host, domain))
    return bool(seen) and bool(wanted) and (seen == wanted or seen.endswith(f".{wanted}"))


def on_site(url: str, base_url: str) -> bool:
    """Whether `url` is plain http(s) on the site's own host or a subdomain. Anything a browser could read differently
    from this parser (userinfo, backslashes, non-ASCII, whitespace) is refused rather than interpreted."""
    if not url.isascii() or _UNSAFE_IN_URL.search(url):
        return False
    try:
        if urlparse(url).scheme not in ("http", "https"):
            return False
        host, base = site_host(url), site_host(base_url)
    except ValueError:
        return False
    return bool(base) and (host == base or host.endswith(f".{base}"))
