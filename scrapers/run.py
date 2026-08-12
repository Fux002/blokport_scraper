"""Scraper runner: run one site, all sites, or a custom order.

    python -m scrapers.run marenostone            # one
    python -m scrapers.run all                     # all (registry order)
    python -m scrapers.run zucchi varsha           # explicit order

Each scraper writes to data/<source>/<timestamp>/products.csv and declares its
format (slab/block/tile) so blocks, slabs, and tiles are tagged correctly per
source. Add a scraper to REGISTRY as it is migrated onto ScraperBase.
"""

from __future__ import annotations

import importlib
import sys
from typing import Type

# Resolve the import path ONCE: `-m scrapers.run` gives the `scrapers.` package; running the file directly
# (`python scrapers/run.py`) does not, so fall back to a bare import with this dir on the path.
try:
    from scrapers.base import ScraperBase
    _PKG = "scrapers."
except ImportError:
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from base import ScraperBase
    _PKG = ""

# ONE source of truth: the sources the runner scrapes. A source is listed ONLY once it is fully onboarded --
# a ScraperBase module AND an adapter + config entry (NEW_SOURCE_CHECKLIST). The class is DISCOVERED from the
# module (mirrors adapter auto-discovery), so there is no import list to keep in sync. Order is the `all` run
# order. slabware (helper) + stonevip (empty) are not scrapers.
# develi / ferraz / fulei / temmer / tureks / brumagran have ScraperBase modules but NO adapter/config yet, so
# they are deliberately NOT listed: an adapter-less source can never reach the pipeline (stone_pipeline.run
# builds its work list from the ADAPTER registry), and listing one here would only let a `scrape all` on an
# unseeded box abort the whole produce on that dormant scraper's failure (rc=1). Add on full onboarding.
_SOURCES = ("marenostone", "polonine", "zucchi", "varsha", "fuleistone")


def _discover(source: str) -> Type[ScraperBase]:
    """The one ScraperBase subclass DEFINED in scrapers/<source>.py (imported bases are ignored)."""
    module = importlib.import_module(f"{_PKG}{source}")
    subs = [obj for obj in vars(module).values()
            if isinstance(obj, type) and issubclass(obj, ScraperBase)
            and obj is not ScraperBase and obj.__module__ == module.__name__]
    if len(subs) != 1:
        raise RuntimeError(
            f"scrapers/{source}.py must define exactly one ScraperBase subclass, found {len(subs)}: {subs}")
    return subs[0]


# source id -> ScraperBase subclass, in _SOURCES order (preserved by dict insertion).
REGISTRY: dict[str, Type[ScraperBase]] = {name: _discover(name) for name in _SOURCES}


def run_one(source: str):
    if source not in REGISTRY:
        raise SystemExit(
            f"scraper '{source}' is not registered. Registered: {sorted(REGISTRY)}."
        )
    return REGISTRY[source]().run()


def _enabled_order() -> list[str]:
    """Registry order for `all`, minus any source the config store has disabled (paused/delisted), so a
    paused vendor is not pointlessly live-scraped. Mirrors stone_pipeline.run.run_all's filter. Falls back
    to the full registry when there is no config store or it can't be read (standalone scraper use)."""
    order = list(REGISTRY)
    try:
        from stone_pipeline.config import store
        enabled = store.enabled_names()          # None == no store yet -> keep every source
    except Exception:
        return order
    if enabled is None:
        return order
    skipped = [s for s in order if s not in enabled]
    if skipped:
        print(f"skipping disabled source(s): {skipped}")
    return [s for s in order if s in enabled]


def run_many(order: list[str]) -> dict[str, str]:
    results: dict[str, str] = {}
    for source in order:
        try:
            results[source] = str(run_one(source))
        except SystemExit as exc:
            print(exc)
        except Exception as exc:  # one site failing never stops the others
            print(f"{source} failed: {exc}")
    return results


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        print("registered:", sorted(REGISTRY))
        return 0
    order = _enabled_order() if argv == ["all"] else argv
    results = run_many(order)
    print(f"\nscraped {len(results)} source(s):")
    for src, path in results.items():
        print(f"  {src}: {path}")
    # A source that was REQUESTED (in order) but produced no result FAILED. Surface it as a non-zero rc:
    # produce._live_scrape and run_pipeline.sh (set -euo pipefail) both rely on this exit code to abort
    # before building against stale/absent data (the docstring at produce._live_scrape). The old always-0
    # silently defeated that abort, so a scrape failure proceeded to build on stale data with no signal.
    # Per-source isolation is unchanged: run_many still attempts EVERY source; only the exit code reflects
    # the failures. An empty `order` (every source disabled) is not a failure.
    failed = [s for s in order if s not in results]
    if failed:
        print(f"\nFAILED to scrape {len(failed)} source(s): {failed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
