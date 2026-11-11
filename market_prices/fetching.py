"""The rules every request obeys: robots.txt first, paced, and a block ends the site's collection at once.

A Transport does the HTTP. This module holds no client on purpose: the one live transport is market_prices.transport,
and everything here runs the same on a test's scripted transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

from market_prices import config
from market_prices.robots import Robots, parse_robots


@dataclass(frozen=True)
class Response:
    status: int
    text: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str:
        return next((value for key, value in self.headers.items() if key.lower() == name.lower()), "")


class Transport(Protocol):
    def get(self, url: str) -> Response:
        """One GET. Raises TransportError when no HTTP answer came back."""


class TransportError(Exception):
    """No HTTP answer: timeout, connection or DNS failure."""


class UnexpectedResponse(Exception):
    """The site answered, but not with what this request needs."""


class SiteBlocked(Exception):
    """The site refused this client. Reported as unavailable, never worked around."""


class SiteDisallowed(Exception):
    """robots.txt forbids this collection."""


def _is_html(response: Response) -> bool:
    return "html" in response.header("content-type").lower() or response.text.lstrip()[:1] == "<"


def _raise_if_blocked(response: Response, url: str) -> None:
    if response.status in config.BLOCK_STATUSES:
        raise SiteBlocked(f"HTTP {response.status} from {url}")
    if response.header(config.CHALLENGE_HEADER):
        raise SiteBlocked(f"challenge header (HTTP {response.status}) from {url}")
    # Only an HTML page can be a challenge: a product or a robots rule may contain these words.
    opening = response.text[:config.CHALLENGE_SCAN_CHARS].lower()
    if _is_html(response) and any(marker in opening for marker in config.CHALLENGE_MARKERS):
        raise SiteBlocked(f"challenge page (HTTP {response.status}) from {url}")


def _target(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path or '/'}?{parts.query}" if parts.query else parts.path or "/"


class SiteFetcher:
    """One site's requests in one run. Construction reads robots.txt, so no page is fetched without it."""

    def __init__(self, robots_url: str, transport: Transport, pause: Callable[[float], None]):
        self._transport = transport
        self._pause = pause
        self._robots = self._read_robots(robots_url)
        if self._robots.crawl_delay > config.MAX_CRAWL_DELAY_SECONDS:
            raise SiteDisallowed(f"robots.txt asks for a crawl delay of {self._robots.crawl_delay}s, "
                                 f"over the cap of {config.MAX_CRAWL_DELAY_SECONDS}s")
        self._interval = max(config.MIN_REQUEST_INTERVAL_SECONDS, self._robots.crawl_delay)

    def _read_robots(self, url: str) -> Robots:
        response = self._transport.get(url)
        if response.status in config.MISSING_ROBOTS_STATUSES:
            return Robots()
        _raise_if_blocked(response, url)
        # Unknown rules are not permission: anything but a real robots file fails closed.
        if response.status != 200:
            raise UnexpectedResponse(f"HTTP {response.status} from {url}")
        if _is_html(response):
            raise UnexpectedResponse(f"not a robots file at {url}")
        if len(response.text) > config.MAX_ROBOTS_CHARS:
            raise UnexpectedResponse(f"robots file at {url} is over {config.MAX_ROBOTS_CHARS} characters")
        return parse_robots(response.text, config.ROBOTS_TOKEN)

    def get(self, url: str) -> Response:
        if not self._robots.allows(_target(url)):
            raise SiteDisallowed(f"robots.txt disallows {url}")
        self._pause(self._interval)
        response = self._transport.get(url)
        _raise_if_blocked(response, url)
        if response.status != 200:
            raise UnexpectedResponse(f"HTTP {response.status} from {url}")
        return response
