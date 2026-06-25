"""Per-source configuration loader (section 3.1, config/sources.yaml).

A per-source value overrides the global default in settings.py. Stage 8 reads
the backend constants from here; Stage 6 reads ports_default and
default_bundle_size; emit reads source_code and emit_on_review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from stone_pipeline.config.settings import SETTINGS


@dataclass
class SourceConfig:
    source: str
    adapter: str = ""
    source_code: str = ""
    # Optional per-scrape owner override; blank -> the general Blokport company (settings/env var).
    # The sales channel is env-level (one per env) and is NOT settable per scrape.
    company_id: str = ""
    ports_default: list[str] = field(default_factory=list)
    # ISO-2 country of the SUPPLIER (the scraped website's company). The origin default
    # when the scrape has no country and origin_map doesn't cover the variety; origin_map
    # (per-variety) overrides it for stones with a known specific origin.
    origin_default: str = ""
    emit_on_review: bool = True
    default_bundle_size: int = 6
    # The source burns a visible watermark into its product photos (e.g. varsha).
    # When true the image stage de-watermarks before re-hosting (local/s3 modes).
    watermarked: bool = False
    # Trust level for auto-loading into Medusa: "review" (default) quarantines the
    # source's output to the stage-for-sign-off lane; "auto" lets it load
    # automatically. Promote review->auto only after `python -m stone_pipeline.certify
    # <source>` is green. New sources stay in review so they can never silently
    # push bad data live.
    mode: str = "review"

    def __post_init__(self):
        # source_code is the SKU prefix ({source_code}-{surrogate}) and the delist scope key. A
        # configured source that omits source_code must still get a usable prefix, or its SKUs
        # become "-{surrogate}" and cross-source delist scoping (+ the 30% cap denominator) break.
        if not (self.source_code or "").strip():
            self.source_code = self.source[:3]


def load_sources(path: Path | None = None) -> dict[str, SourceConfig]:
    path = Path(path or SETTINGS.paths.sources_yaml)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, SourceConfig] = {}
    for source, body in data.items():
        body = body or {}
        out[source] = SourceConfig(source=source, **body)
    return out


def load_source(source: str, path: Path | None = None) -> SourceConfig:
    config = load_sources(path).get(source)
    if config is None:
        # a source with no config still runs with the global defaults
        return SourceConfig(source=source, source_code=source[:3])
    return config
