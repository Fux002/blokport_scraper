"""The dev pilot: ONE named approved site of ONE brand, run against development's bucket, then a summary printed (each
site's status, rows and malformed listings, and the first rows): `python -m market_prices.pilot`.

Config-driven, no arguments: config.PILOT_BRAND and config.PILOT_SITE_ID. It refuses any environment but development,
and any site the brand's register does not hold or has not approved, before a single request. It runs the run entry's
own path (run.run_live) on a register of that one site, so the pilot shows exactly what a full run would do for it.
"""

from __future__ import annotations

from pathlib import Path

from market_prices import config, run
from market_prices.register import Register, RegisterError, load_register
from stone_pipeline.config.settings import IS_PRODUCTION


def pilot_register(brand: str, site_id: str, sites_dir: Path = config.SITES_DIR) -> Register:
    """The brand's register cut to the one site, which must be approved."""
    register = load_register(brand, sites_dir)
    site = next((site for site in register.sites if site.site_id == site_id), None)
    if site is None:
        raise RegisterError(f"the {brand} register has no site {site_id!r}")
    if not site.collectable:
        raise RegisterError(f"{brand} site {site_id!r} is not approved: its decision is {site.decision.value}")
    return Register(brand=brand, sites=(site,))


def main() -> None:
    if IS_PRODUCTION:
        raise SystemExit("the market-price pilot runs only in development, against dev's bucket")
    outcome = run.run_live(pilot_register(config.PILOT_BRAND, config.PILOT_SITE_ID))
    print(run.summary(outcome, sample=config.PILOT_SAMPLE_ROWS))


if __name__ == "__main__":
    main()
