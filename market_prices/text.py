"""Turn hostile site text into one clean line, keeping the seller's words."""

from __future__ import annotations

import html
import re
from typing import Optional
from urllib.parse import urlparse

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: object) -> Optional[str]:
    """Tags removed, entities decoded, whitespace collapsed; None when nothing is left. Tags go before entities
    are decoded, so an escaped "&lt;b&gt;" that the seller wrote as text survives as text."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    text = html.unescape(_TAG.sub(" ", str(value)))
    return _SPACE.sub(" ", _CONTROL.sub(" ", text)).strip() or None


def site_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def on_site(url: str, base_url: str) -> bool:
    """Whether `url` is http(s) on the site's own host or a subdomain of it."""
    if urlparse(url).scheme not in ("http", "https"):
        return False
    host, base = site_host(url), site_host(base_url)
    return bool(base) and (host == base or host.endswith(f".{base}"))
