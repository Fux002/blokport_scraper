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
from stone_pipeline.core.text import title_case
from stone_pipeline.matching import projections as proj

# Field names the catalog stages build/read. Kept here so curate.py and emit stay unchanged; the store
# persists these same fields as the pending-queue payload.
ATTR_COLUMNS = ["medusa_id", "kind", "value", "count", "suggested_value", "action"]
# src/image/description are the scraped EVIDENCE for the review UI: a human assigning the correct type
# (which of the Gneiss/Granite/Marble/Onyx 'Aqua Blue' this product is) needs to see the actual product,
# not just the name. Empty when the row that raised the hold carried none. Not display-cased (URLs/free text).
CONFIRM_COLUMNS = ["confirm", "variant", "reason", "stone_type", "color", "nearest_existing",
                   "score", "model_prob", "src", "src_url", "image", "description"]
_PENDING_VARIETY_FIELDS = [c for c in CONFIRM_COLUMNS if c != "confirm"]   # 'confirm' now lives as `action`
# scrape-derived display names title-cased at the write boundary (uniform casing regardless of curate path).
# nearest_existing is included: the code-shaped hold writes a raw supplier-cased base there while the
# alias-review/typeless holds write a canonical existing-variety Name -- same column, so normalize it too.
_DISPLAY_CASE_FIELDS = frozenset({"variant", "color", "nearest_existing"})
# The backbone-leaf suggestion: a value Medusa already has, not yet allowed on this variety. Same fields
# in the CSV audit artifact and the review-queue payload, one source of truth.
LEAF_COLUMNS = ["variety", "stone_type", "attribute", "add_value", "currently_allowed",
                "match_method", "match_confidence", "verdict", "example_url"]


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


def load_alias_types() -> dict[str, str]:
    """norm(spelling) -> the alias TARGET's stone type, where the operator chose one. Disambiguates a
    multi-type target name so the spelling aliases into the right stone (paired with load_alias_decisions)."""
    return decisions_store.alias_type_map()


def load_rejected() -> set[str]:
    """Varieties the operator said 'no' to before -- never propose them again."""
    return decisions_store.rejected_names()


def load_variety_seed_colors() -> dict[str, str]:
    """norm(variant) -> the operator-chosen mint colour. curate seeds a minted variety with this instead
    of the generic 'Natural' fallback, so a colourless source does not leave the variety colourless."""
    return decisions_store.variety_seed_colors()


def load_variety_seed_types() -> dict[str, str]:
    """norm(variant) -> the operator-assigned stone type. curate mints a type-less variety with this
    instead of holding it, so a source that supplies no type does not leave the variety unmintable."""
    return decisions_store.variety_seed_types()


def save_rejected(rejected: set[str]) -> None:
    """Persist runtime-learned rejects. Never overwrites an explicit mint/alias decision."""
    decisions_store.learn_rejects(rejected)


def write_confirm_file(pending: list[dict]) -> int:
    """Replace the pending VARIETY queue with this run's still-undecided varieties. Returns the count.
    A decided variety is simply absent from `pending`, so it stops appearing.

    Title-case the display names (variant + colour) HERE, at the one boundary every builder funnels through,
    so the queue is uniformly cased no matter which curate path produced the row. The curate builders were
    inconsistent -- the mint/typeless paths title-cased, but the code-shaped and alias-review holds wrote the
    raw supplier casing, so a source shouting 'VENATTO BLUE' showed up in caps. The `ref` stays norm(variant)
    (case-insensitive), so a decision keyed by norm still routes regardless of display casing. stone_type is
    left as-is (already the resolved canonical value; title_case would mangle a hyphenated/multi-word type)."""
    def _payload(row: dict) -> dict:
        return {c: (title_case(str(row.get(c) or "")) if c in _DISPLAY_CASE_FIELDS else row.get(c, ""))
                for c in _PENDING_VARIETY_FIELDS}
    rows = [{"ref": _norm(row.get("variant", "")), "payload": _payload(row), "sources": row.get("sources")}
            for row in pending if _norm(row.get("variant", ""))]
    decisions_store.replace_pending("variety", rows)
    return len(rows)


def write_origin_confirm_file(rows) -> int:
    """Replace the pending ORIGIN queue with this run's rows held for origin confirmation (a vendor whose
    primary_origin the per-variety map did not corroborate). ONE entry per (source, variety, type) -- keyed
    so it is per-vendor and per-identity, separate from the variety mint queue. A resolved origin is simply
    absent next run, so it stops appearing (same lifecycle as the variety queue). Returns the count."""
    from stone_pipeline.core.schema import FlagCode
    pending: dict[str, dict] = {}
    for row in rows:
        if getattr(row, "origin_source", "") != "origin_needs_confirmation":
            continue
        variety = (row.variation_name or row.raw_name or "").strip()
        stone_type = (row.type_name or "").strip()
        source = (row.src_site or "").strip()
        if not (source and variety and stone_type):
            continue
        ref = f"{_norm(source)}|{_norm(variety)}|{_norm(stone_type)}"
        if ref in pending:
            continue                             # first occurrence per (source, variety, type) is enough
        flag = next((f for f in row.review_flags if f.code == FlagCode.origin_needs_confirmation), None)
        # Carry the product EVIDENCE, same as the mint queue's review card: the supplier listing url so the
        # operator can OPEN the product to judge the origin, and one product image. Prefer the raw supplier
        # url/image (survives Medusa product churn) exactly like curate._review_evidence.
        imgs = (getattr(row, "raw_image_urls", None) or []) or (getattr(row, "image_keys", None) or [])
        pending[ref] = {
            "ref": ref,
            "payload": {"source": source, "variety": title_case(variety), "stone_type": stone_type,
                        "map_country": (flag.raw_value if flag else ""),
                        "vendor_origin": (flag.best_guess if flag else ""),
                        "src_url": (getattr(row, "src_url", "") or ""),
                        "image": next((u for u in imgs if u), "")},
            "sources": [source],
        }
    decisions_store.replace_pending("origin", list(pending.values()))
    return len(pending)


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


# -- backbone leaf-growth suggestions ------------------------------------------

def write_backbone_leaf_pending(pending: list[dict]) -> int:
    """Replace the pending BACKBONE-LEAF queue with this run's still-undecided value additions (a value
    Medusa already has, not yet allowed on the variety). A tuple the operator already approved or rejected
    is dropped, so it stops appearing (mirrors the variety/attribute queues). Returns the count."""
    decided = decisions_store.leaf_decided()
    rows = []
    for r in pending:
        variety, attribute, value = r.get("variety", ""), r.get("attribute", ""), r.get("add_value", "")
        if not (variety and attribute and value):
            continue
        keytuple = (_norm(variety), _norm(r.get("stone_type", "")), attribute, _norm(value))
        if keytuple in decided:
            continue
        # title-case the display VARIETY name here too, so this queue matches the variety queue (the export
        # casing it comes from is not reliably title-cased). The `ref` stays norm-keyed, so routing is
        # unaffected. add_value/currently_allowed are canonical resolved values already; stone_type left as-is.
        payload = {c: r.get(c, "") for c in LEAF_COLUMNS}
        payload["variety"] = title_case(str(payload.get("variety") or ""))
        rows.append({"ref": "|".join(keytuple), "payload": payload, "sources": r.get("sources")})
    decisions_store.replace_pending("backbone_leaf", rows)
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
