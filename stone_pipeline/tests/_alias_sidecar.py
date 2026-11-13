"""The alias sidecar emit writes beside 1_variants_full.csv (aliases.payload.ALIAS_SIDECAR), for tests that hand-write a
full file and populate the ledger from it: each row's Aliases as `seed` alias_rows and no codes. Without it a
variety's alias_rows stay NULL (not computed) and the serve holds it (ledger v18, P2-10 C4). No em dashes."""
from __future__ import annotations

import csv
from pathlib import Path

from stone_pipeline.aliases import payload as alias_payload
from stone_pipeline.aliases.surfaces import Surfaces


def write_alias_sidecar(full: Path) -> Path:
    """The sidecar beside `full`, from its Key and Aliases columns; returns `full`."""
    with Path(full).open(encoding="utf-8-sig", newline="") as h:
        rows = {r["Key"]: alias_payload.rows_of(Surfaces(aliases=tuple(
            (a, "seed") for a in (r.get("Aliases") or "").split("|") if a)))
            for r in csv.DictReader(h) if r.get("Key")}
    alias_payload.write_sidecar(Path(full).parent, rows)
    return Path(full)
