"""synonyms/<vocab>.csv: raw (normalized) spelling -> canonical backend value."""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path

from stone_pipeline.reference.loaders.common import env, norm


@lru_cache(maxsize=None)
def load_synonyms(vocab: str, directory: Path | None = None) -> dict[str, str]:
    """raw (normalized) -> canonical backend value, or 'none' to resolve to no id
    without an error (section 7, Stage 3). Missing file is an empty map. Cached: it is read once per
    vocab and consulted per-row (resolver + name-detection), and synonyms are static within a run."""
    directory = Path(directory or env().SETTINGS.paths.synonyms_dir)
    path = directory / f"{vocab}.csv"
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            raw = norm(record.get("raw") or "")
            canon = (record.get("canonical") or "").strip()
            if raw and canon:
                mapping[raw] = canon
    return mapping
