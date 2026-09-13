"""Attribute curation: the unresolved colour/finish/type/quality values of a run, each with the nearest
existing canonical value and a synonym-vs-new recommendation. A closed vocabulary, so an unresolved value
is almost always a synonym; only rarely a genuinely new value (which goes to the attribute queue)."""

from __future__ import annotations

import csv
from pathlib import Path

from stone_pipeline.core.schema import CanonicalRow, FlagCode
from stone_pipeline.reference.loaders import ReferenceData
from stone_pipeline.stages.normalize import AttributeResolvers


def build_attribute_curation(rows: list[CanonicalRow], ref: ReferenceData) -> list[dict]:
    """Colour/finish/type/quality are a CLOSED vocabulary, so an unresolved value
    is almost always a SYNONYM of an existing value (pipeline-side, no Medusa id
    needed), and only rarely a genuinely new value (which goes to the attribute
    file). Aggregate distinct unresolved values per vocab, suggest the nearest
    existing canonical value, and recommend synonym vs new_value."""
    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        for flag in row.review_flags:
            if flag.code == FlagCode.attr_unresolved and flag.raw_value:
                key = (flag.field, flag.raw_value.strip())
                seen[key] = seen.get(key, 0) + 1

    # The SAME resolver Stage 3 uses, so the review suggestion never drifts from resolution.
    resolvers = AttributeResolvers.build(ref)
    out: list[dict] = []
    for (vocab, raw_value), count in sorted(seen.items()):
        resolver = resolvers.resolvers.get(vocab)
        if resolver is None:
            continue    # attribute curation only handles the closed attr vocab (type/colour/finish/
                        # quality); a 'variation' unresolved is a variety, owned by the confirm queue
        # resolve() already found the single nearest canonical (evidence); keep it before resolve_multi
        # tries to compose, so the plain-fuzzy fallback reuses that score instead of recomputing it.
        res = resolver.resolve(raw_value)
        nearest = res.evidence or {}
        if res.value is None and res.method != "synonym_none":
            res = resolver.resolve_multi(raw_value)
        if res.value is not None:                       # maps onto an existing value -> adopt as a synonym
            best, action, score = res.value, "synonym", 100.0
        elif res.method == "compound_suggest":          # a real, new compound to create ('A and B')
            best, action, score = res.evidence["best"], "new_value", 90.0
        else:                                           # single unresolved -> resolve()'s fuzzy nearest
            best, score = nearest.get("best") or "", nearest.get("score", 0.0)
            action = "synonym" if score >= 80 else "synonym?" if score >= 60 else "new_value"
        out.append({
            "vocab": vocab, "raw_value": raw_value, "count": count,
            "suggested_value": best, "score": score, "recommended_action": action,
        })
    return out


def write_attribute_curation(attr_rows: list[dict], outputs_dir: Path, run_id: str) -> Path | None:
    if not attr_rows:
        return None
    path = Path(outputs_dir) / f"curation_attributes_{run_id}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["vocab", "raw_value", "count", "suggested_value", "score", "recommended_action"],
        )
        writer.writeheader()
        writer.writerows(attr_rows)
    return path
