"""Adapter self-test runner (section 7A.2).

Each adapter ships, under adapters/fixtures/<source>/, a small committed input
sample (input.csv) and the expected canonical output (expected.json). This
runner replays every fixture and asserts the adapter still produces the expected
canonical rows. A new adapter is "done" when its fixture passes; a repaired
adapter is verified the instant its fixture passes again.
"""

from __future__ import annotations

import json
from pathlib import Path

from stone_pipeline.adapters.base import AdapterBase, read_scrape_csv
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core.schema import CanonicalRow

# adapter source id -> adapter instance
from stone_pipeline.adapters.marenostone import ADAPTER as MARENOSTONE
from stone_pipeline.adapters.polonine import ADAPTER as POLONINE
from stone_pipeline.adapters.varsha import ADAPTER as VARSHA
from stone_pipeline.adapters.zucchi import ADAPTER as ZUCCHI

REGISTRY: dict[str, AdapterBase] = {
    POLONINE.source: POLONINE,
    MARENOSTONE.source: MARENOSTONE,
    ZUCCHI.source: ZUCCHI,
    VARSHA.source: VARSHA,
}


def fixture_dir(source: str) -> Path:
    return SETTINGS.paths.fixtures_dir / source


def row_to_comparable(row: CanonicalRow) -> dict:
    """Compare only the fields an adapter is responsible for (src_ and raw_ plus
    adapter_version). Downstream fields are stage outputs, not adapter outputs."""
    data = row.model_dump(mode="json")
    keep_prefixes = ("src_", "raw_", "variety_match_key", "adapter_version", "scrape_timestamp")
    return {k: v for k, v in data.items() if k.startswith(keep_prefixes) or k == "variety_match_key"}


def regenerate_fixture(source: str, sample_input: Path) -> None:
    """Capture the expected canonical output for a committed input sample
    (onboarding step 4 / repair step 3). Writes expected.json next to input.csv."""
    adapter = REGISTRY[source]
    frame = read_scrape_csv(sample_input)
    rows = adapter.adapt(frame)
    expected = [row_to_comparable(r) for r in rows]
    out_dir = fixture_dir(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "expected.json").write_text(
        json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def run_fixture(source: str) -> tuple[bool, str]:
    adapter = REGISTRY[source]
    fdir = fixture_dir(source)
    frame = read_scrape_csv(fdir / "input.csv")
    produced = [row_to_comparable(r) for r in adapter.adapt(frame)]
    expected = json.loads((fdir / "expected.json").read_text(encoding="utf-8"))
    if produced != expected:
        # find the first differing row for a useful message
        for index, (got, want) in enumerate(zip(produced, expected)):
            if got != want:
                diffs = {k: (got.get(k), want.get(k)) for k in set(got) | set(want) if got.get(k) != want.get(k)}
                return False, f"row {index} differs: {diffs}"
        return False, f"row count differs: produced {len(produced)} expected {len(expected)}"
    return True, f"{len(produced)} rows match"


def run_all() -> dict[str, tuple[bool, str]]:
    return {source: run_fixture(source) for source in REGISTRY}
