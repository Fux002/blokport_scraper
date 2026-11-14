"""The supplier prices a produce's own scrape read (platform supplier_scrape). For each source the produce scraped
that the brand's register reads prices from, that scrape's folder is collected and delivered as its own run, stamped
with the scrape's time. One fetch, one transport: nothing here requests a site. Generic: the register's rows select the
sources (supplier_key), never a source named in code."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional, TypeVar

from market_prices import config
from market_prices.collect import RunResult, collect_supplier_run
from market_prices.collectors import COLLECTORS
from market_prices.collectors.supplier_scrape import read_scrape
from market_prices.register import Register

Delivered = TypeVar("Delivered")


def supplier_sources(register: Register, scraped: Optional[Iterable[str]]) -> list[str]:
    """The sources whose scrape feeds this register's supplier_scrape rows, among those a produce scraped (None: every
    source it scrapes)."""
    keys = {site.supplier_key for site in register.sites if not COLLECTORS[site.platform].fetches}
    return sorted(keys if scraped is None else keys & set(scraped))


def newest_scrape(source_dir: Path, since: str) -> Optional[Path]:
    """The source's newest run folder stamped at or after `since` (written by this produce) whose price pass ran (its
    marker is there, whatever it states); None when there is none. An older folder is a previous produce's, never read
    as this one's."""
    if not source_dir.is_dir():
        return None
    for folder in sorted((path for path in source_dir.iterdir() if path.is_dir() and path.name >= since),
                         key=lambda path: path.name, reverse=True):
        if (folder / config.SCRAPE_MARKER_FILE).is_file():
            return folder
    return None


def emit(register: Register, scraped: Optional[Iterable[str]], data_dir: Path, since: str,
         deliver: Callable[[RunResult], Delivered]) -> list[Delivered]:
    """Each fed source's run from this produce's price pass of it, delivered; a source whose price pass did not run in
    this produce delivers nothing (no price was seen)."""
    delivered = []
    for source in supplier_sources(register, scraped):
        folder = newest_scrape(data_dir / source, since)
        if folder is not None:
            delivered.append(deliver(collect_supplier_run(register, read_scrape(folder))))
    return delivered


def emit_live(scraped: Optional[Iterable[str]], since: str) -> list[str]:
    """The produce's call: this deployment's brand register, its data folder, delivered to its bucket. One line per
    fed source, for the produce's log: its run and each site's status, or that this produce left no scrape of it."""
    from market_prices import run
    from market_prices.register import load_register
    from stone_pipeline.config.settings import BRAND, SETTINGS

    register = load_register(BRAND)
    lines = []
    for source in supplier_sources(register, scraped):
        outcomes = emit(register, [source], SETTINGS.paths.data_dir, since, run.deliver)
        if not outcomes:
            lines.append(f"{source}: no price pass in this produce, no supplier prices delivered")
        for outcome in outcomes:
            sites = ", ".join(f"{status.site_id} {status.status.value} ({status.rows} rows)"
                              for status in outcome.result.statuses)
            lines.append(f"{source}: run {outcome.result.run_id}: {sites}")
    return lines
