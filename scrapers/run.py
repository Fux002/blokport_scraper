"""Scraper runner: run one site, all sites, or a custom order.

    python -m scrapers.run marenostone            # one
    python -m scrapers.run all                     # all (registry order)
    python -m scrapers.run zucchi develi ferraz    # explicit order

Each scraper writes to data/<source>/<timestamp>/products.csv and declares its
format (slab/block/tile) so blocks, slabs, and tiles are tagged correctly per
source. Add a scraper to REGISTRY as it is migrated onto ScraperBase.
"""

from __future__ import annotations

import sys
from typing import Type

try:
    from scrapers.base import ScraperBase
    from scrapers.brumagran import BrumagranScraper
    from scrapers.develi import DeveliScraper
    from scrapers.ferraz import FerrazScraper
    from scrapers.fulei import FuleiScraper
    from scrapers.marenostone import MarenoStoneScraper
    from scrapers.polonine import PolonineScraper
    from scrapers.temmer import TemmerScraper
    from scrapers.tureks import TureksScraper
    from scrapers.varsha import VarshaScraper
    from scrapers.zucchi import ZucchiScraper
except ImportError:
    import os
    sys.path.insert(0, os.path.dirname(__file__))
    from base import ScraperBase
    from brumagran import BrumagranScraper
    from develi import DeveliScraper
    from ferraz import FerrazScraper
    from fulei import FuleiScraper
    from marenostone import MarenoStoneScraper
    from polonine import PolonineScraper
    from temmer import TemmerScraper
    from tureks import TureksScraper
    from varsha import VarshaScraper
    from zucchi import ZucchiScraper

# source id -> ScraperBase subclass. slabware (Playwright, polonine + tenants) and
# stonevip (empty) are not migrated yet.
REGISTRY: dict[str, Type[ScraperBase]] = {
    "marenostone": MarenoStoneScraper,
    "polonine": PolonineScraper,
    "zucchi": ZucchiScraper,
    "develi": DeveliScraper,
    "ferraz": FerrazScraper,
    "fulei": FuleiScraper,
    "temmer": TemmerScraper,
    "tureks": TureksScraper,
    "brumagran": BrumagranScraper,
    "varsha": VarshaScraper,
}


def run_one(source: str):
    if source not in REGISTRY:
        raise SystemExit(
            f"scraper '{source}' is not registered. Registered: {sorted(REGISTRY)}."
        )
    return REGISTRY[source]().run()


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
    order = list(REGISTRY) if argv == ["all"] else argv
    results = run_many(order)
    print(f"\nscraped {len(results)} source(s):")
    for src, path in results.items():
        print(f"  {src}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
