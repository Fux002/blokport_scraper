"""Human decisions read back into the pipeline.

The catalog READS operator decisions at the start of every run and REWRITES the pending review queue at
the end. For each uncertain variety the catalog proposes, the operator chooses (in the :4200 admin, via
the config review API) one of:

    mint   -> the catalog mints it (adds it to the upload + backbone) and it drops off the queue
    reject -> permanently rejected; remembered so it is never proposed again
    alias  -> it is really a spelling of an existing variety X; the product routes onto X

Decisions live in config.db (see config/decisions_store.py) -- ONE durable, snapshotted store, not a CSV
under ephemeral /app. This module is the produce-side FACADE over that store: it keeps the field names
and function names the catalog stages already call, so the storage swap is invisible to them. New
colours/finishes/types work the same way: the operator pastes the Medusa id and the catalog adopts it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from stone_pipeline.config import decisions_store
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.matching import projections as proj

# Field names the catalog stages build/read. Kept here so curate.py and emit stay unchanged; the store
# persists these same fields as the pending-queue payload.
ATTR_COLUMNS = ["medusa_id", "kind", "value", "count", "suggested_value", "action"]
CONFIRM_COLUMNS = ["confirm", "variant", "stone_type", "color", "nearest_existing", "score", "model_prob"]
_PENDING_VARIETY_FIELDS = [c for c in CONFIRM_COLUMNS if c != "confirm"]   # 'confirm' now lives as `action`


def _norm(s: str) -> str:
    return proj.norm(s or "")


# -- variety decisions ---------------------------------------------------------

def load_confirm_decisions() -> dict[str, str]:
    """norm(variant) -> 'yes' | 'no' for decided varieties (mint -> yes, reject -> no). alias decisions
    are consumed separately via load_alias_decisions."""
    return decisions_store.confirm_map()


def load_alias_decisions() -> dict[str, str]:
    """norm(spelling) -> the existing variety NAME it should alias onto."""
    return decisions_store.alias_map()


def load_rejected() -> set[str]:
    """Varieties the operator said 'no' to before -- never propose them again."""
    return decisions_store.rejected_names()


def save_rejected(rejected: set[str]) -> None:
    """Persist runtime-learned rejects. Never overwrites an explicit mint/alias decision."""
    decisions_store.learn_rejects(rejected)


def write_confirm_file(pending: list[dict]) -> int:
    """Replace the pending VARIETY queue with this run's still-undecided varieties. Returns the count.
    A decided variety is simply absent from `pending`, so it stops appearing."""
    rows = [{"ref": _norm(row.get("variant", "")),
             "payload": {c: row.get(c, "") for c in _PENDING_VARIETY_FIELDS},
             "sources": row.get("sources")}
            for row in pending if _norm(row.get("variant", ""))]
    decisions_store.replace_pending("variety", rows)
    return len(rows)


# -- retired variation keys (already durable in config.db) ---------------------

def load_retired() -> set[str]:
    """The retired variation KEYS -- the ONE exclusion source, backed by config.db (durable: snapshotted +
    restored, so a retired variety never re-mints after a restart). Every produce stage that could
    re-introduce a variety reads this; un-retire removes the key."""
    from stone_pipeline.config import store
    return store.load_retired()


def add_retired(key: str) -> None:
    from stone_pipeline.config import store
    store.add_retired(key)


def remove_retired(key: str) -> None:
    from stone_pipeline.config import store
    store.remove_retired(key)


# -- attribute decisions -------------------------------------------------------

def load_attribute_ids() -> dict[tuple[str, str], tuple[str, str]]:
    """(kind, norm(value)) -> (ORIGINAL value, medusa_id) for the ids the operator pasted. The original
    value (operator casing) becomes the CANONICAL attribute name, not its normalization."""
    return decisions_store.attribute_ids()


def write_attributes_to_add(pending: list[dict]) -> int:
    """Replace the pending ATTRIBUTE queue with values still missing a Medusa id. Returns the count."""
    rows = [{"ref": f"{(r.get('kind') or '').strip().lower()}:{_norm(r.get('value', ''))}",
             "payload": {c: r.get(c, "") for c in ATTR_COLUMNS},
             "sources": r.get("sources")}
            for r in pending if (r.get("kind") and r.get("value"))]
    decisions_store.replace_pending("attribute", rows)
    return len(rows)


def adopt_attribute_ids(filled: dict[tuple[str, str], tuple[str, str]],
                        attributes_csv: Path | None = None) -> int:
    """Append the (kind, value, id) the operator filled into the env's attributes.csv vocab so the next
    run knows them. Returns how many were adopted (skips ones already present). Writes the ORIGINAL value
    (operator casing) as the canonical name -- normalization is for the dedup key only."""
    path = Path(attributes_csv or SETTINGS.paths.attributes_csv)
    if not filled or not path.exists():
        return 0
    existing = set()
    with path.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            existing.add(((r.get("category") or "").strip().lower(), _norm(r.get("value", ""))))
    new = [(k, raw_value, mid) for (k, vnorm), (raw_value, mid) in filled.items()
           if (k, vnorm) not in existing]
    if not new:
        return 0
    with path.open("a", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        for kind, raw_value, mid in new:
            w.writerow([kind, raw_value, mid])
    return len(new)
