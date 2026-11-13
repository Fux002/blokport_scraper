"""The wood smoke run, executed by test_wood_smoke_run.py in a fresh interpreter inside the tmp wood workspace (with
SCRAPER_DOMAIN_PACK=wood), so the pack, the categories built from it at import and every path are wood's own.

It stands in for what a deployment does: Medusa's variants export is published (the seed with ids; here Id = Key, the
loader's own rule), the config server's boot creates config.db from sources.yaml, a deployment with ledger
write-through seeds its ledger from it as the sync server does, then the produce runs the source and the catalog.
The only stub is the image I/O every fixture run stubs (conftest scrape_data_dir): a manifest that knows every url,
an empty discard pool. No pack value or stone logic is touched."""

from __future__ import annotations

import csv
import hashlib

from stone_pipeline import catalog
from stone_pipeline.config import store
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.io import imagestore
from stone_pipeline.ledger import bootstrap, writethrough
from stone_pipeline.ledger.db import Ledger
from stone_pipeline.run import run_source
from stone_pipeline.stages import images


class _EveryUrlImproved(dict):
    def get(self, url, default=None):
        digest = hashlib.sha256(url.encode()).hexdigest()
        return f"https://fixtures.invalid{imagestore.IMPROVED_MARKER}test/{digest}.png"

    def __bool__(self):
        return True


def _medusa_export() -> None:
    """from_medusa/<env>/variants_export.csv as Medusa would publish it for the seed: every variety with its Id."""
    paths = SETTINGS.paths
    with paths.variants_export_base_csv.open(encoding="utf-8", newline="") as src, \
            paths.variants_export_csv.open("w", encoding="utf-8", newline="") as dst:
        reader = csv.DictReader(src)
        writer = csv.DictWriter(dst, fieldnames=["Id"] + reader.fieldnames)
        writer.writeheader()
        for row in reader:
            writer.writerow({"Id": row["Key"], **row})


def main() -> None:
    images._readonly_manifest = _EveryUrlImproved
    images._load_discard_set = lambda cfg=None: set()
    _medusa_export()
    store.seed_from_yaml()   # the config server's boot creates config.db (restore, then seed); a produce never does
    if writethrough.enabled():   # a deployment with write-through: the sync server seeds its ledger first
        with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as ledger:
            bootstrap.seed_attributes(ledger)
            bootstrap.seed_variations(ledger)
    run_source("woodfeed")
    catalog.run()


if __name__ == "__main__":
    main()
