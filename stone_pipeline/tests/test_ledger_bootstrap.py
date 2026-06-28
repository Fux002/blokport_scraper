"""Bootstrap seeding: the ledger absorbs the Medusa id foundation (design 5B).

Proves the seeders load the from_medusa exports and that the id lookups everything
id-bearing depends on (attribute name -> id, variation Key -> id) resolve. Skips
cleanly when the exports are absent, so it never blocks a run.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.ledger.bootstrap import attribute_id, seed_attributes, seed_variations
from stone_pipeline.ledger.db import Ledger

ATTRS = SETTINGS.paths.attributes_csv
EXPORT = SETTINGS.paths.variants_export_csv


@pytest.mark.skipif(not ATTRS.exists(), reason="no attributes.csv to seed from")
def test_seed_attributes_synced_with_ids(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        n = seed_attributes(ledger)
        assert n > 100, f"expected the full attribute vocabulary, got {n}"
        # every seeded attribute is synced (it already exists in Medusa)
        assert ledger.counts("attribute") == {"synced": n}
        # the lookup products and combinations resolve ids through works
        beige = attribute_id(ledger, "color", "Beige")
        assert beige and len(beige) >= 20, f"color/Beige did not resolve to an id: {beige!r}"


@pytest.mark.skipif(not EXPORT.exists(), reason="no variants_export.csv to seed from")
def test_seed_variations_carry_medusa_ids(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as ledger:
        n = seed_variations(ledger)
        assert n > 20000, f"expected the full variant catalog, got {n}"
        # a variation carries its real Medusa id and is synced after bootstrap
        row = ledger.execute(
            "SELECT medusa_id, state, branch FROM variation WHERE key LIKE 'block_marble_%' "
            "AND medusa_id IS NOT NULL LIMIT 1"
        ).fetchone()
        assert row is not None, "no block_marble variation seeded with an id"
        assert row["state"] == "synced"
        assert row["branch"] == "block"
        assert len(row["medusa_id"]) >= 20, f"medusa_id does not look like an id: {row['medusa_id']!r}"
