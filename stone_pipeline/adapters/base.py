"""AdapterBase and the adapter framework (section 7 Stage 1, section 7A).

An adapter is thin and mostly declarative (section 7A.1): a field map from source
columns (or small extraction rules) to canonical src_ and raw_ fields. AdapterBase
owns the canonical schema, the required-field assertions, the variety_match_key
declaration, and the shared parsing helpers, so an adapter author writes only what
is genuinely source-specific.

The adapter does only column mapping and light parsing. No normalization, no
matching, no derivation, and it does not build title, description, handle, or
slug; those are generated downstream in Stage 6 (section 7 Stage 1).
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Callable, Optional, Union

import polars as pl

from stone_pipeline.config.contracts import SourceContract, generate_contract_from_sample
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.core.text import clean_variety_name, detect_code_prefixes

log = logfmt.get_logger("adapter")

# A field map entry is either a source column name (str) or a callable that takes
# the source row dict and returns the canonical value.
FieldRule = Union[str, Callable[[dict[str, Any]], Any]]

# How a source's raw frame is ACQUIRED (the Acquire layer). A scraper is just one kind of data source.
ACQ_SCRAPER = "scraper"        # a fetcher in scrapers.REGISTRY wrote data/<source>/<ts>/products.csv
ACQ_LOAD_FRAME = "load_frame"  # the adapter fetches its own frame (API / DB / partner feed)
ACQ_FILE_DROP = "file_drop"    # an operator drops an export file; no fetcher, read like a scrape


class AdapterBase:
    """Subclass and set: source, adapter_version, variety_match_key (the source
    column carrying the variety name, may be None for generic-descriptor sources),
    required_columns (source columns Stage 0 must see), and field_map."""

    source: str = ""
    adapter_version: str = "0.0.0"
    variety_match_key: Optional[str] = None
    # the scrape column that carries the explicit block/slab/tile tag (the scraper
    # should set this); None when the source has no format tag yet
    format_field: Optional[str] = None
    # True for sources whose product name is a generic colour+type descriptor with
    # no clean variety column. The matcher then auto-accepts only deterministic
    # (exact/projection) variation hits and routes fuzzy/phonetic ones to review.
    generic_descriptor: bool = False
    # source-specific code-prefix regexes the generic name cleanup can't infer (e.g. varsha's
    # collapsed 'Z'/'ZB'). Generic artifacts (lone-letter / alphanumeric leading codes) are
    # stripped for EVERY source without listing them here.
    code_prefixes: tuple[str, ...] = ()
    _lead_codes: frozenset = frozenset()  # data-discovered per batch in adapt()
    required_columns: list[str] = []
    # canonical field -> source column or callable
    field_map: dict[str, FieldRule] = {}
    # canonical fields that must be non-empty after mapping, else the row is bad
    required_canonical: tuple[str, ...] = ("src_natural_key",)
    # explicit acquisition override ("" -> infer). Set to ACQ_FILE_DROP for a source whose export an
    # operator drops in with no fetcher; load_frame sources are inferred, so no config is needed for them.
    acquisition: str = ""

    def acquires_via(self) -> str:
        """How this source's raw frame is acquired (the Acquire layer): 'load_frame' when the adapter
        fetches it (API/DB/feed), else 'scraper'. Inferred from a load_frame override unless set
        explicitly, so an all-scraper source needs no new config and behaves exactly as before."""
        if self.acquisition:
            return self.acquisition
        return ACQ_LOAD_FRAME if type(self).load_frame is not AdapterBase.load_frame else ACQ_SCRAPER

    # --- shared parsing helpers (section 7A.1) --------------------------------
    @staticmethod
    def clean(value: Any) -> str:
        if value is None:
            return ""
        # decode HTML entities (&#8211; -> en-dash, &amp; -> &) so scraped names match
        # the reference, which stores decoded names -- else 'Marjan &#8211; No. 426'
        # never matches the variant 'Marjan – No. 426' and the product is dropped.
        text = html.unescape(str(value)).replace("\xa0", " ")   # non-breaking space -> normal space
        text = re.sub("[​-‍﻿]", "", text)       # drop zero-width chars / BOM
        return text.strip()

    @staticmethod
    def strip_trailing_token(value: Any, token: str = "/") -> str:
        """Drop a trailing render or grade tag such as polonine's 'Granite /'."""
        text = AdapterBase.clean(value)
        text = re.sub(rf"\s*{re.escape(token)}\s*$", "", text)
        return text.strip()

    @staticmethod
    def split_list(value: Any, sep: str = "|") -> list[str]:
        text = AdapterBase.clean(value)
        if not text:
            return []
        return [part.strip() for part in text.split(sep) if part.strip()]

    @staticmethod
    def first_of(value: Any, sep: str = "|") -> str:
        parts = AdapterBase.split_list(value, sep)
        return parts[0] if parts else ""

    # --- the engine -----------------------------------------------------------
    def _apply_rule(self, rule: FieldRule, record: dict[str, Any]) -> Any:
        if callable(rule):
            return rule(record)
        return record.get(rule)

    def adapt_record(self, record: dict[str, Any]) -> Optional[CanonicalRow]:
        data: dict[str, Any] = {"src_site": self.source, "adapter_version": self.adapter_version}
        for canonical_field, rule in self.field_map.items():
            data[canonical_field] = self._apply_rule(rule, record)
        # declare the variety match key value only when the field map did not set
        # it at all. An empty string set by the map is intentional (a generic
        # descriptor with no named variety) and must not fall back to the raw
        # column, or the gap routing for those rows is defeated.
        if self.variety_match_key and "variety_match_key" not in data:
            data["variety_match_key"] = self.clean(record.get(self.variety_match_key))
        # honour the declared format column when the map didn't set raw_format, so a new
        # adapter only needs `format_field = "format"` (no boilerplate field_map entry).
        if self.format_field and "raw_format" not in data:
            data["raw_format"] = self.clean(record.get(self.format_field))
        # carry the scraper's reserved per-row fetch-failure signal through automatically, so EVERY source
        # gets "hold, never default a fetch-failed dimension" with zero per-adapter work. Absent column ->
        # empty list. ("fetch_failed" == scrapers.base.ScraperBase.FETCH_FAILED_COL.)
        if "fetch_failed_fields" not in data:
            data["fetch_failed_fields"] = [g for g in self.clean(record.get("fetch_failed")).split("|") if g]
        # smart name cleanup: strip supplier-code artifacts so matching AND minting see a
        # clean variety name: generic structural rules + data-discovered codes (_lead_codes)
        # + any explicit code_prefixes override -- so codes are removed without hardcoding them.
        if data.get("variety_match_key"):
            data["variety_match_key"] = clean_variety_name(
                data["variety_match_key"], self.code_prefixes, self._lead_codes)
        row = CanonicalRow.model_validate(data)
        for required in self.required_canonical:
            if not getattr(row, required, None):
                return None
        return row

    def load_frame(self, scrape_path: Optional[Path] = None):
        """Ingest hook for NON-FILE data sources (API / DB / partner feed). Override to return
        ``(frame, timestamp_token, origin_label)`` -- the records as a polars frame, a timestamp
        string for the run id, and a label for logging. Everything downstream (adapt -> stages ->
        emit) is identical regardless of where the frame came from.

        Default returns ``None``, so the run reads the standard
        ``data/<source>/<timestamp>/products.csv`` (the scraper path). A new tabular source needs
        only an adapter; a non-file source additionally overrides this one method."""
        return None

    def adapt(self, frame: pl.DataFrame) -> list[CanonicalRow]:
        """Row-isolated mapping (section 13A.3): a malformed row is dropped, the
        batch continues; the count of dropped rows is logged."""
        # DISCOVER this source's leading code prefixes from the whole batch (data-driven, no
        # hardcoded strings), so adapt_record strips them from every variety -- generalises to
        # a new scraper's own codes without listing them.
        if self.variety_match_key and self.variety_match_key in frame.columns:
            names = [self.clean(v) for v in frame[self.variety_match_key].to_list()]
            self._lead_codes = detect_code_prefixes(names)
        rows: list[CanonicalRow] = []
        dropped = 0
        for record in frame.iter_rows(named=True):
            try:
                row = self.adapt_record(dict(record))
            except Exception as exc:  # isolate the bad row
                dropped += 1
                log.warning(
                    "adapter dropped a row",
                    extra={"extra_fields": {"source": self.source, "error": str(exc)}},
                )
                continue
            if row is None:
                # required canonical field empty (e.g. blank variety/material/name). Symmetric with
                # the exception path above: log it so the drop is never silent.
                dropped += 1
                log.warning(
                    "adapter dropped a row: empty required canonical field(s)",
                    extra={"extra_fields": {"source": self.source,
                                            "required_canonical": list(self.required_canonical),
                                            "sku": record.get("sku"), "name": record.get("name"),
                                            "permalink": record.get("permalink")}},
                )
                continue
            rows.append(row)
        log.info(
            f"adapted {len(rows)} rows ({dropped} dropped) for {self.source}",
            extra={"extra_fields": {"source": self.source, "version": self.adapter_version}},
        )
        return rows

    def smoke_count(self, sample: pl.DataFrame) -> int:
        """Count valid canonical rows produced from a sample, for the Stage 0
        parse smoke test (section 7 check 5)."""
        return len(self.adapt(sample))

    def generate_contract(self, frame: pl.DataFrame) -> SourceContract:
        """The field map generates the source's contract entry (section 7A.1),
        keeping the health contract and the adapter in sync by construction."""
        contract = generate_contract_from_sample(
            self.source, frame, self.required_columns, self.adapter_version
        )
        return contract


def normalize_header(frame: pl.DataFrame) -> pl.DataFrame:
    """Strip a leading UTF-8 BOM from the first column name (common in these
    scrape exports) so the field map matches by plain column names."""
    if frame.width and frame.columns[0].startswith("﻿"):
        frame = frame.rename({frame.columns[0]: frame.columns[0].lstrip("﻿")})
    return frame


def read_scrape_csv(path) -> pl.DataFrame:
    """Read a scrape file as all-strings (adapters do their own light parsing)."""
    frame = pl.read_csv(path, infer_schema_length=0)
    return normalize_header(frame)
