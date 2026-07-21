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
    # (company_id is a Medusa id, used only by the legacy CSV path.)
    company_id: str = ""
    # The COMPANY / vendor this source's products belong to (the marketplace seller). This is
    # the AGNOSTIC reference sent as `vendor` in the sync payload; Medusa resolves it to its own
    # company id. This is how you set "which scraper belongs to which company": set it per source
    # here (several sources may share one company). Blank falls back to the source name.
    vendor: str = ""
    ports_default: list[str] = field(default_factory=list)
    # ISO-2 country of the SUPPLIER (the scraped website's company). Used by derive_origin
    # as the LOW-confidence fallback (flagged origin_supplier_default) when the scrape has no
    # country and origin_map doesn't cover the variety -- Medusa requires an origin for its
    # pricing-rule lookup. origin_map (per-variety) overrides it for known specific origins.
    origin_default: str = ""
    emit_on_review: bool = True
    default_bundle_size: int = 6
    # The source burns a visible watermark into its product photos (e.g. varsha).
    # When true the image stage de-watermarks before re-hosting (local/s3 modes).
    watermarked: bool = False
    # Upscale (Real-ESRGAN, GPU) this source's photos before re-host. A per-source switch like
    # watermarked, set from the admin UI -- default on. When off the GPU upscale is skipped; images are
    # still size-reduced (CPU, no GPU) and re-hosted. Independent of watermarked.
    enhance: bool = True
    # Trust level for auto-loading into Medusa: "review" (default) quarantines the
    # source's output to the stage-for-sign-off lane; "auto" lets it load
    # automatically. Promote review->auto only after `python -m stone_pipeline.certify
    # <source>` is green. New sources stay in review so they can never silently
    # push bad data live.
    mode: str = "review"
    # Catastrophic-scrape floor: if a run yields fewer than this many rows it is treated as a failed
    # scrape (auth/endpoint broke, truncated) -- the run aborts and NEVER feeds discontinuation, so a
    # broken fetch can't mass-delist the catalog. Set well below the normal count (it guards against
    # near-empty scrapes, not normal supplier inventory variation). 0 = only the empty-scrape guard.
    min_expected_rows: int = 0

    def __post_init__(self):
        # source_code is the SKU prefix ({source_code}-{surrogate}) and the delist scope key. A
        # configured source that omits source_code must still get a usable prefix, or its SKUs
        # become "-{surrogate}" and cross-source delist scoping (+ the 30% cap denominator) break.
        if not (self.source_code or "").strip():
            self.source_code = self.source[:3]
        # normalize once at the boundary: the SKU prefix is uppercased at emit but the `source` column
        # (populate) and the delist/seed prefix (.lower()) and the reset scope key must all agree, so a
        # non-lowercase config can never split a source's rows across two `source` values.
        self.source_code = self.source_code.strip().lower()


def load_yaml_sources(path: Path | None = None) -> dict[str, SourceConfig]:
    """The committed seed: sources straight from sources.yaml (no config store)."""
    path = Path(path or SETTINGS.paths.sources_yaml)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, SourceConfig] = {}
    for source, body in data.items():
        body = body or {}
        out[source] = SourceConfig(source=source, **body)
    # source_code is the SKU prefix AND the delist scope key: two sources sharing one (e.g. an auto
    # source[:3] collision like marenostone->'mar' and a future 'marble'->'mar') would scope each
    # other's products for discontinuation and pollute the 30% cap. Refuse to load on a collision.
    by_code: dict[str, str] = {}
    for src, cfg in out.items():
        clash = by_code.get(cfg.source_code)
        if clash:
            raise ValueError(f"duplicate source_code {cfg.source_code!r} for {clash!r} and {src!r}; "
                             f"set an explicit unique source_code in sources.yaml")
        by_code[cfg.source_code] = src
    return out


def load_sources(path: Path | None = None) -> dict[str, SourceConfig]:
    """Source config, preferring the durable config store (the control plane the admin
    UI edits) and falling back to sources.yaml. An explicit `path` always reads YAML
    (tests, the seed). The DB holds the same agnostic settings, env-independent."""
    if path is None:
        from stone_pipeline.config import store  # lazy: avoids an import cycle
        if store.config_db_path().exists():
            return store.read_sources()
    return load_yaml_sources(path)


def load_source(source: str, path: Path | None = None) -> SourceConfig:
    config = load_sources(path).get(source)
    if config is None:
        # a source with no config still runs with the global defaults
        return SourceConfig(source=source, source_code=source[:3])
    return config
