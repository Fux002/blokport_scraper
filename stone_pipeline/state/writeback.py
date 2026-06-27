"""Outbound write-back (section 8.4).

A confirmed fuzzy/projection/review variation match appends the scraped spelling
to that variation's alias list. This is what makes the system more consistent every
run instead of re-reviewing the same names forever. Write-backs are append-only and
recorded in the manifest. (Origin is NOT written back automatically -- origin_map.csv
is hand-maintained in catalog_source/; the origin review flag is the worklist.)

Rather than mutate the live backend exports (which can be re-pulled), confirmed
aliases are appended to state/alias_writeback.csv. The variation index loader
reads this file in addition to the exports, so the next run resolves the spelling
as an exact alias hit for free.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS

ALIAS_WRITEBACK = "alias_writeback.csv"


def _writeback_path(name: str) -> Path:
    return SETTINGS.paths.state_dir / name


@dataclass
class WriteBack:
    """Accumulates confirmed learnings during a run; flushed append-only at the end."""

    aliases: list[tuple[str, str]] = field(default_factory=list)  # (variation_id, alias)
    seen_aliases: set[tuple[str, str]] = field(default_factory=set)

    def add_alias(self, variation_id: str, alias: str) -> None:
        alias = (alias or "").strip()
        if not variation_id or not alias:
            return
        key = (variation_id, alias.casefold())
        if key in self.seen_aliases:
            return
        self.seen_aliases.add(key)
        self.aliases.append((variation_id, alias))

    def flush(self, path: Path | None = None) -> int:
        """Append new aliases to the write-back file, skipping ones already there.
        Returns the count actually written. Append-only and idempotent."""
        if not self.aliases:
            return 0
        path = Path(path or _writeback_path(ALIAS_WRITEBACK))
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = load_alias_writeback(path)
        existing_keys = {(vid, alias.casefold()) for vid, alias in existing}
        new = [(vid, alias) for vid, alias in self.aliases
               if (vid, alias.casefold()) not in existing_keys]
        if not new:
            return 0
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if write_header:
                writer.writerow(["variation_id", "alias"])
            for vid, alias in new:
                writer.writerow([vid, alias])
        return len(new)


def load_alias_writeback(path: Path | None = None) -> list[tuple[str, str]]:
    path = Path(path or _writeback_path(ALIAS_WRITEBACK))
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for record in csv.DictReader(handle):
            vid = (record.get("variation_id") or "").strip()
            alias = (record.get("alias") or "").strip()
            if vid and alias:
                out.append((vid, alias))
    return out
