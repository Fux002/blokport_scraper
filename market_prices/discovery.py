"""A site's entry points, as its register row's `discovery` names them: the sitemaps it lists and the listing pages
(with the pages their rel="next" links lead to). Shared by every collector that reads pages (schema_org_product,
declared_html), so one site is walked one way. Every scan is linear: an opening tag, then the next closing tag after
it, resuming where the last stopped, so a hostile page cannot stall the run. The caps are config's SCHEMA_ORG_*."""

from __future__ import annotations

import html
import re
import zlib
from typing import TYPE_CHECKING, Iterator, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from market_prices import config
from market_prices.fetching import Response, SiteFetcher, UnexpectedResponse
from market_prices.text import on_site

if TYPE_CHECKING:
    from market_prices.register import Site

_LOC_OPEN = re.compile(r"<loc\s*>", re.IGNORECASE)
_LOC_CLOSE = re.compile(r"</loc\s*>", re.IGNORECASE)
_SITEMAP_INDEX = re.compile(r"<sitemapindex\b", re.IGNORECASE)
# [^<>]: a tag ends at the next '<' as well, so an unclosed one fails at once instead of scanning the rest of the page
_LINK_TAG = re.compile(r"<(?:a|link)\b[^<>]*>", re.IGNORECASE)
_HREF = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
_REL_NEXT = re.compile(r"""\brel\s*=\s*(?:"[^"]*\bnext\b[^"]*"|'[^']*\bnext\b[^']*'|next\b)""", re.IGNORECASE)
_GZIP_MAGIC = b"\x1f\x8b"


def between(text: str, opening: re.Pattern, closing: re.Pattern) -> Iterator[tuple[str, str]]:
    """Each (opening tag, text up to the next closing tag), in page order. Once no closing tag follows an opening, no
    later item can close either, so the scan stops."""
    at = 0
    while (opened := opening.search(text, at)) is not None:
        closed = closing.search(text, opened.end())
        if closed is None:
            return
        yield opened.group(0), text[opened.end():closed.start()]
        at = closed.end()


def normal(url: str) -> str:
    """The URL without its fragment: the same page however it was linked."""
    return urlunsplit(urlsplit(url)._replace(fragment=""))


def _sitemap_text(response: Response, url: str) -> str:
    """The sitemap's text; a gzipped file (served as a .gz, not as a transfer encoding) is unzipped under a cap."""
    if response.body[:2] != _GZIP_MAGIC:
        return response.text
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(response.body, config.SCHEMA_ORG_MAX_UNZIPPED_BYTES + 1)
    except zlib.error as exc:
        raise UnexpectedResponse(f"{url} is not a readable gzip file") from exc
    if len(data) > config.SCHEMA_ORG_MAX_UNZIPPED_BYTES:
        raise UnexpectedResponse(f"{url} unzipped is over the cap of {config.SCHEMA_ORG_MAX_UNZIPPED_BYTES} bytes")
    if inflater.unused_data:    # zlib reads one member; a second would otherwise be left unread, silently
        raise UnexpectedResponse(f"{url} holds more than one gzip member")
    return data.decode("utf-8", errors="replace")


def sitemap_pages(site: "Site", fetcher: SiteFetcher) -> set[str]:
    """Every page the entry sitemaps list. An index's sitemaps on the site are followed, breadth first, under a count
    and a depth cap; one off the site is never requested."""
    pages: set[str] = set()
    queue = [(f"{site.base_url}{path}", 0) for path in site.discovery.sitemaps]
    seen: set[str] = set()
    while queue:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if len(seen) > config.SCHEMA_ORG_MAX_SITEMAPS:
            raise UnexpectedResponse(f"the entry points list over {config.SCHEMA_ORG_MAX_SITEMAPS} sitemaps")
        text = _sitemap_text(fetcher.get(url), url)
        locs = [normal(urljoin(url, html.unescape(loc.strip()))) for _, loc in between(text, _LOC_OPEN, _LOC_CLOSE)]
        if not _SITEMAP_INDEX.search(text):
            pages.update(locs)
            continue
        if depth >= config.SCHEMA_ORG_MAX_SITEMAP_DEPTH:
            raise UnexpectedResponse(f"sitemap index nested deeper than {config.SCHEMA_ORG_MAX_SITEMAP_DEPTH} at {url}")
        queue.extend((loc, depth + 1) for loc in locs if on_site(loc, site.base_url))
    return pages


def _href(tag: str) -> Optional[str]:
    found = _HREF.search(tag)
    return next((group for group in found.groups() if group is not None), None) if found else None


def listing_pages(site: "Site", fetcher: SiteFetcher) -> Iterator[tuple[str, str, set[str]]]:
    """Each entry listing page and the pages its rel="next" links lead to, under a page cap, as (url, text, the links
    on it). A next link off the site, or back to a page already read, ends that entry's pages."""
    seen: set[str] = set()
    for path in site.discovery.listing_pages:
        url: Optional[str] = f"{site.base_url}{path}"
        while url is not None and url not in seen:
            seen.add(url)
            if len(seen) > config.SCHEMA_ORG_MAX_LISTING_PAGES:
                raise UnexpectedResponse(f"the entry points lead to over {config.SCHEMA_ORG_MAX_LISTING_PAGES} "
                                         f"listing pages")
            text = fetcher.get(url).text
            links: set[str] = set()
            following = None
            for tag in _LINK_TAG.findall(text):
                href = _href(tag)
                if href is None:
                    continue
                target = normal(urljoin(url, html.unescape(href)))
                if _REL_NEXT.search(tag):
                    following = following or target
                else:
                    links.add(target)
            yield url, text, links
            url = following if following is not None and on_site(following, site.base_url) else None


def product_urls(site: "Site", fetcher: SiteFetcher) -> list[str]:
    """The product pages the entry points lead to: on the site, the path matching product_path, sorted."""
    pattern = re.compile(site.discovery.product_path)
    found = sitemap_pages(site, fetcher) | {link for _url, _text, links in listing_pages(site, fetcher)
                                            for link in links}
    return sorted(url for url in found if on_site(url, site.base_url) and pattern.search(urlsplit(url).path))
