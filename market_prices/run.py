"""One market-price run for this deployment's brand: `python -m market_prices.run` (RUN_MODE=market-prices on the
ad-hoc task). No arguments: the brand, environment, bucket and S3 dry run come from settings (SCRAPER_BRAND,
SCRAPER_ENV, SCRAPER_S3_BUCKET, SCRAPER_S3_DRY_RUN), and every tunable from market_prices/config.py.

It loads the brand's register (a register that does not validate stops the run before any request), collects every
site in it over the live transport (only an approved site is requested at all), writes the run folder and publishes it
with publish_run. A run with no approved site, or no rows, is published as evidence and leaves latest where it is.
Under the S3 dry run the folder is written and nothing is uploaded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from market_prices import config
from market_prices.collect import RunResult, collect_run, write_run
from market_prices.fetching import Transport
from market_prices.publish import publish_run
from market_prices.register import Register, load_register
from market_prices.transport import LiveTransport
from stone_pipeline.config.settings import BRAND, ENV_NAME, S3_BUCKET, SETTINGS
from stone_pipeline.io.storage import BOTOCORE_DEFAULTS, aws_client


@dataclass(frozen=True)
class RunOutcome:
    result: RunResult
    run_dir: Path
    published: Optional[dict]       # None under the S3 dry run


def run_register(register: Register, transport: Transport, client, bucket: str, root: Path, moment: datetime,
                 dry_run: bool, pause: Callable[[float], None]) -> RunOutcome:
    """Collect, write and (unless a dry run) publish one register's run: the one path the run entry and the pilot
    share."""
    result = collect_run(register, transport, moment, pause)
    run_dir = write_run(result, root)
    return RunOutcome(result, run_dir, None if dry_run else publish_run(client, bucket, register.brand, run_dir))


def summary(outcome: RunOutcome, sample: int = 0) -> str:
    """One line per site (status, rows, malformed, why), then the first `sample` rows, then whether it was published."""
    lines = [f"run {outcome.result.run_id} of {outcome.result.brand}: {outcome.run_dir}"]
    for status in outcome.result.statuses:
        detail = f" ({status.detail})" if status.detail else ""
        lines.append(f"{status.site_id}: {status.status.value}, {status.rows} rows, "
                     f"{status.malformed_listings} malformed{detail}")
    for row in outcome.result.observations[:sample]:
        price = f"{row.price_amount} {row.currency or ''}".strip() if row.price_amount else row.price_text or "-"
        lines.append(f"  {row.site_id} {row.listing_key}: {row.listing_text} | {price} | {row.stated_specs}")
    if outcome.published is None:
        lines.append("published: no (S3 dry run)")
    else:
        lines.append(f"published: yes, latest moved: {outcome.published['latest_moved']}")
    return "\n".join(lines)


def _dry_run() -> bool:
    return SETTINGS.s3.dry_run


def _s3_client():
    return aws_client("s3", config=BOTOCORE_DEFAULTS)


def _runs_root() -> Path:
    return config.RUNS_DIR / ENV_NAME


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_live(register: Register) -> RunOutcome:
    """`register` collected over the live transport and published to this deployment's bucket."""
    dry_run = _dry_run()
    with LiveTransport() as transport:
        return run_register(register, transport, None if dry_run else _s3_client(), S3_BUCKET, _runs_root(), _now(),
                            dry_run, time.sleep)


def main() -> None:
    print(summary(run_live(load_register(BRAND))))


if __name__ == "__main__":
    main()
