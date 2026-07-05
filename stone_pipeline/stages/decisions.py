"""Human decisions read back into the pipeline.

The review files are no longer dead-end outputs: `variants_to_confirm.csv` is a decision ledger the
catalog READS at the start of every run. For each uncertain variety the catalog proposes, you set
the `confirm` column:

    true   -> the catalog mints it (adds it to the upload + backbone) and drops the row
    false  -> permanently rejected; remembered in state so it is never proposed again
    (blank)-> still pending; stays in the file

So the pipeline learns your decisions without you hand-editing the backbone. `attributes_to_add.csv`
works the same way for new colours/finishes/types/qualities: you paste the Medusa id you created and
the catalog adopts it.
"""

from __future__ import annotations

import csv
from pathlib import Path

from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.core import csvio
from stone_pipeline.matching import projections as proj

ATTR_FILE = "attributes_to_add.csv"
ATTR_COLUMNS = ["medusa_id", "kind", "value", "count", "suggested_value", "action"]
CONFIRM_FILE = "variants_to_confirm.csv"
CONFIRM_COLUMNS = ["confirm", "variant", "stone_type", "color", "nearest_existing", "score", "model_prob"]
_REJECTED_STATE = "rejected_varieties.csv"
_TRUE = {"true", "yes", "y", "1", "x"}
_FALSE = {"false", "no", "n", "0"}


def _norm(s: str) -> str:
    return proj.norm(s or "")


def load_confirm_decisions(path: Path | None = None) -> dict[str, str]:
    """norm(variant) -> 'yes' | 'no' for rows the user has marked; blank rows are omitted."""
    path = Path(path or SETTINGS.paths.review_dir / CONFIRM_FILE)
    out: dict[str, str] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            v = (r.get("confirm") or "").strip().lower()
            name = _norm(r.get("variant", ""))
            if not name:
                continue
            if v in _TRUE:
                out[name] = "yes"
            elif v in _FALSE:
                out[name] = "no"
    return out


def _rejected_path(path: Path | None = None) -> Path:
    return Path(path or SETTINGS.paths.state_dir / _REJECTED_STATE)


def load_rejected(path: Path | None = None) -> set[str]:
    """Varieties the user has said 'no' to before — never propose them again."""
    path = _rejected_path(path)
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as h:
        return {_norm(r[0]) for r in csv.reader(h) if r and r[0].strip()}


def save_rejected(rejected: set[str], path: Path | None = None) -> None:
    path = _rejected_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # headerless (load_rejected reads col 0); atomic + formula-injection-safe on the scraped names.
    csvio.atomic_write(path, lambda h: csv.writer(h).writerows(
        [csvio.safe_cell(name)] for name in sorted(rejected)))


_RETIRED_STATE = "retired_keys.csv"


def _retired_path(path: Path | None = None) -> Path:
    return Path(path or SETTINGS.paths.state_dir / _RETIRED_STATE)


def load_retired(path: Path | None = None) -> set[str]:
    """Variation KEYS an operator explicitly retired -- never re-mint or re-serve them. Keyed by Key
    (not name, unlike load_rejected) because a retire targets the canonical variation Key. This is the
    ONE exclusion source for retired varieties (tree_build reads it into exclude_ids, E10); un-retire
    removes the key so the next produce can re-mint it (mirrors source resume)."""
    path = _retired_path(path)
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig", newline="") as h:
        return {r[0].strip() for r in csv.reader(h) if r and r[0].strip()}


def save_retired(retired: set[str], path: Path | None = None) -> None:
    path = _retired_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    csvio.atomic_write(path, lambda h: csv.writer(h).writerows(
        [csvio.safe_cell(key)] for key in sorted(retired)))


def add_retired(key: str, path: Path | None = None) -> None:
    retired = load_retired(path)
    retired.add(key)
    save_retired(retired, path)


def remove_retired(key: str, path: Path | None = None) -> None:
    retired = load_retired(path)
    if key in retired:
        retired.discard(key)
        save_retired(retired, path)


def load_attribute_ids(path: Path | None = None) -> dict[tuple[str, str], tuple[str, str]]:
    """(kind, norm(value)) -> (ORIGINAL value, medusa_id), for rows where you filled in the id you
    created in Medusa. The original value (operator's casing/punctuation) is preserved so it becomes
    the CANONICAL attribute name, not its lowercased normalization."""
    path = Path(path or SETTINGS.paths.review_dir / ATTR_FILE)
    out: dict[tuple[str, str], tuple[str, str]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            mid = (r.get("medusa_id") or "").strip()
            kind = (r.get("kind") or "").strip().lower()
            value = (r.get("value") or "").strip()
            if mid and kind and value:
                out[(kind, _norm(value))] = (value, mid)
    return out


def write_attributes_to_add(pending: list[dict], path: Path | None = None) -> Path:
    """Rewrite the attribute ledger with values still missing a Medusa id (you create them in
    Medusa, paste the id, and next run the pipeline adopts it into attributes.csv and drops the row)."""
    path = Path(path or SETTINGS.paths.review_dir / ATTR_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    # operator-edited ledger (you paste Medusa ids) with scraped values -> sanitize + atomic.
    csvio.write_dicts(path, ATTR_COLUMNS, [{c: r.get(c, "") for c in ATTR_COLUMNS} for r in pending],
                      sanitize=True)
    return path


def adopt_attribute_ids(filled: dict[tuple[str, str], tuple[str, str]], attributes_csv: Path | None = None) -> int:
    """Append the (kind, value, id) the user filled into the env's attributes.csv so the vocab
    knows them on the next run. Returns how many were adopted (skips ones already present). Writes the
    ORIGINAL value (operator casing) as the canonical name -- normalization is for the dedup key only."""
    path = Path(attributes_csv or SETTINGS.paths.attributes_csv)
    if not filled or not path.exists():
        return 0
    existing = set()
    with path.open(encoding="utf-8-sig", newline="") as h:
        for r in csv.DictReader(h):
            existing.add(((r.get("category") or "").strip().lower(), _norm(r.get("value", ""))))
    new = [(k, raw_value, mid) for (k, vnorm), (raw_value, mid) in filled.items() if (k, vnorm) not in existing]
    if not new:
        return 0
    with path.open("a", newline="", encoding="utf-8") as h:
        w = csv.writer(h)
        for kind, raw_value, mid in new:
            w.writerow([kind, raw_value, mid])
    return len(new)


def write_confirm_file(pending: list[dict], path: Path | None = None) -> Path:
    """Rewrite the decision ledger with the still-pending varieties (blank `confirm`). Applied
    (true) and rejected (false) rows are gone; newly-discovered uncertain ones appear with a blank
    confirm for you to decide next."""
    path = Path(path or SETTINGS.paths.review_dir / CONFIRM_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    # operator-edited decision ledger with scraped variety names -> sanitize + atomic.
    csvio.write_dicts(path, CONFIRM_COLUMNS, [{c: row.get(c, "") for c in CONFIRM_COLUMNS} for row in pending],
                      sanitize=True)
    return path
