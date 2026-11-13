"""The brand a market-price entry works on: the run input config.RUN_BRAND_ENV, read before anything else.

It is never the deployment's own brand, so one deployment's ad-hoc task runs any brand's register or intake. It is
returned as given: the brand's register or layouts loader validates it, and refuses a malformed or unknown one before
any file, bucket or site is read.
"""

from __future__ import annotations

from market_prices import config
from stone_pipeline.core import env


def run_brand() -> str:
    """The run input's brand. Missing or empty stops the entry, naming the variable to set."""
    brand = env.getenv(config.RUN_BRAND_ENV)
    if not brand:
        raise SystemExit(f"{env.PREFIX}{config.RUN_BRAND_ENV} is required: the brand whose market_prices register "
                         f"or intake this run works on (the slug of its market_prices/sites/<brand>.yaml)")
    return brand
