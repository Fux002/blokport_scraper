"""A variety's alias_rows and codes as the payload states them (P2-10 C4; table 173 F9, table 189 C7), and the sidecar
emit writes beside 1_variants_full.csv for the ledger populate to read (the icon sidecar's pattern). Emit computes them
once (decision D1); populate never recomputes them. No em dashes."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from stone_pipeline.aliases.surfaces import Surfaces
from stone_pipeline.core.text import alias_key, match_key

ALIAS_SIDECAR = "variety_alias_rows.json"
# the scraper's own methods: the hand rows (admin) are the backend's and are never sent back
SENT_METHODS = ("seed", "operator", "learned")


def rows_of(surfaces: Surfaces) -> tuple[list[dict], list[str]]:
    """(alias_rows, codes) of one variety: the scraper's values only, alias_rows ordered by alias_key (the backend
    aliasKey's paired twin; match_key for an all-Latin value) and codes by match_key, so the same set always serves,
    and hashes, the same."""
    return (ordered_rows((v, m) for v, m in surfaces.aliases if m in SENT_METHODS),
            ordered_codes(v for v, m in surfaces.codes if m in SENT_METHODS))


def ordered_rows(pairs: Iterable[tuple[str, str]]) -> list[dict]:
    """alias_rows [{value, method}] in the served order: by alias_key, then the value."""
    return sorted(({"value": v, "method": m} for v, m in pairs), key=lambda r: (alias_key(r["value"]), r["value"]))


def ordered_codes(values: Iterable[str]) -> list[str]:
    """codes in the served order: by match_key, then the value."""
    return sorted(values, key=lambda v: (match_key(v), v))


def write_sidecar(directory: Path, by_key: dict[str, tuple[list[dict], list[str]]]) -> Path:
    """{Key: {alias_rows, codes}} beside the produced variety file, sorted by Key so the same set writes the same
    bytes."""
    path = Path(directory) / ALIAS_SIDECAR
    doc = {key: {"alias_rows": rows, "codes": codes} for key, (rows, codes) in sorted(by_key.items())}
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def read_sidecar(directory: Path) -> dict[str, dict] | None:
    """The sidecar beside a variety file, or None when there is none (the base a reset populates from): populate then
    SETS its varieties' alias_rows and codes NULL, not computed, overwriting any earlier value, until a produce
    writes a sidecar."""
    path = Path(directory) / ALIAS_SIDECAR
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
