"""The rules every request obeys: robots.txt first, and a block ends the site's collection at once.

A Transport does the HTTP. This module holds no client on purpose: the live transport arrives with the fetch-layer
split, so until then nothing in this package can reach a real site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.robotparser import RobotFileParser

from market_prices import config


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
    """robots.txt forbids this URL."""


def _is_challenge(response: Response) -> bool:
    if response.header(config.CHALLENGE_HEADER):
        return True
    if "json" in response.header("content-type").lower():
        return False
    opening = response.text[:config.CHALLENGE_SCAN_CHARS].lower()
    return any(marker in opening for marker in config.CHALLENGE_MARKERS)


def _raise_if_blocked(response: Response, url: str) -> None:
    if response.status in config.BLOCK_STATUSES:
        raise SiteBlocked(f"HTTP {response.status} from {url}")
    if _is_challenge(response):
        raise SiteBlocked(f"challenge page (HTTP {response.status}) from {url}")


class SiteFetcher:
    """One site's requests in one run. Construction reads robots.txt, so no page is fetched without it."""

    def __init__(self, robots_url: str, transport: Transport):
        self._transport = transport
        self._robots = self._read_robots(robots_url)

    def _read_robots(self, robots_url: str) -> RobotFileParser:
        response = self._transport.get(robots_url)
        _raise_if_blocked(response, robots_url)
        rules = RobotFileParser()
        if response.status in config.MISSING_ROBOTS_STATUSES:
            rules.parse([])
        elif response.status == 200:
            rules.parse(response.text.splitlines())
        else:
            # Unknown rules are not permission: fail closed.
            raise UnexpectedResponse(f"HTTP {response.status} from {robots_url}")
        return rules

    def get(self, url: str) -> Response:
        if not self._robots.can_fetch(config.USER_AGENT, url):
            raise SiteDisallowed(url)
        response = self._transport.get(url)
        _raise_if_blocked(response, url)
        if response.status != 200:
            raise UnexpectedResponse(f"HTTP {response.status} from {url}")
        return response
