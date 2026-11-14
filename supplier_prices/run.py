"""The supplier prices a produce's own scrape read: for each source it scraped whose price pass ran in this produce,
that pass written and published as the vendor's run. One vendor per run, so one vendor's failure holds back only its
own run. Nothing here requests a site."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from botocore.exceptions import BotoCoreError, ClientError

from scrapers.base import PRICES_MARKER
from stone_pipeline.core.logfmt import get_logger
from supplier_prices.publish import RunAlreadyPublished, publish_run
from supplier_prices.writer import NoPrices, write_run

log = get_logger("supplier_prices")

_RUN_FAILURES = (NoPrices, ValueError, RunAlreadyPublished, ClientError, BotoCoreError, OSError,
                 json.JSONDecodeError)


def newest_pass(source_dir: Path, since: str) -> Optional[Path]:
    """The source's newest run folder stamped at or after `since` (this produce's) whose price pass ran, whatever its
    marker states; None when there is none. An older folder is a previous produce's, never read as this one's."""
    if not source_dir.is_dir():
        return None
    folders = sorted((p for p in source_dir.iterdir() if p.is_dir() and p.name >= since), key=lambda p: p.name)
    return next((f for f in reversed(folders) if (f / PRICES_MARKER).is_file()), None)


def deliver(sources: Optional[Iterable[str]], since: str, *, data_dir: Path, out_root: Path, client, bucket: str,
            brand: str) -> list[dict]:
    """Each source's run from its newest price pass in this produce, written under out_root and published (a None
    client writes only: the S3 dry run). A source that fails is logged and the others go on."""
    names = sorted(sources) if sources is not None else sorted(p.name for p in data_dir.iterdir() if p.is_dir())
    delivered = []
    for source in names:
        folder = newest_pass(data_dir / source, since)
        if folder is None:
            continue
        try:
            written = write_run(folder, out_root, brand)
            published = client is not None and bool(publish_run(client, bucket, brand, written))
        except _RUN_FAILURES as exc:
            log.error(f"supplier prices of {source} not delivered: {type(exc).__name__}: {exc}")
            continue
        delivered.append({"source": source, "run_id": written.run_id, "rows": written.rows,
                          "left_out": written.left_out, "duplicates": written.duplicates, "published": published})
    return delivered
