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
    company_id: str = ""
    sales_channel_id: str = ""
    ports_default: list[str] = field(default_factory=list)
    emit_on_review: bool = True
    default_bundle_size: int = 6
    # The source burns a visible watermark into its product photos (e.g. varsha).
    # When true the image stage de-watermarks before re-hosting (local/s3 modes).
    watermarked: bool = False


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
