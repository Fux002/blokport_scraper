"""Stage 3: normalize controlled vocabulary (section 7 Stage 3).

Produce type_id, color_id, finish_id, quality_id from raw values against the
closed vocabulary. Normalize-then-lookup, not record linkage. Per field, run the
attribute ladder: override (M9), multi-value split, synonym, exact, fuzzy,
unresolved. Never guess: an unresolved value writes a null id and a review flag,
it does not reach output.

The resolved type here is provisional. Stage 5 overrides it from the matched
variety, which is authoritative (section 6A).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from stone_pipeline.adapters.tokens import explicit_type_word
from stone_pipeline.config.settings import SETTINGS, Confidence
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.matching.engine import VocabResolver
from stone_pipeline.reference.loaders import ReferenceData

log = logfmt.get_logger("normalize")

# vocab -> (raw field, code, multi-value allowed)
VOCAB_FIELDS = ("type", "color", "finish", "quality")
_MULTI_SPLIT = re.compile(r"\s*(?:\||/|,| and )\s*", flags=re.IGNORECASE)


@dataclass
class AttributeResolvers:
    resolvers: dict[str, VocabResolver]

    @classmethod
    def build(cls, ref: ReferenceData) -> "AttributeResolvers":
        floor = SETTINGS.thresholds.attribute_fuzzy_floor
        resolvers = {
            vocab: VocabResolver(
                vocab=vocab,
                canonical_values=ref.attributes.canonical_names(vocab),
                synonyms=ref.synonyms.get(vocab, {}),
                fuzzy_floor=floor,
            )
            for vocab in VOCAB_FIELDS
        }
        return cls(resolvers=resolvers)


def _split_multi(raw: str) -> tuple[str, bool]:
    """Return (first_value, is_multi). 'Black | Grey' -> ('Black', True)."""
    parts = [p for p in _MULTI_SPLIT.split(raw or "") if p.strip()]
    if len(parts) <= 1:
        return (raw or "").strip(), False
    return parts[0].strip(), True


def _confidence_name(conf: Confidence) -> str:
    return Confidence(conf).name


def _override_name(ref: ReferenceData, row: CanonicalRow, vocab: str) -> str | None:
    if ref.overrides is None:
        return None
    return ref.overrides.get(row.src_site, row.surrogate_key or "", f"{vocab}_name")


def normalize_row(row: CanonicalRow, resolvers: AttributeResolvers, ref: ReferenceData) -> None:
    for vocab in VOCAB_FIELDS:
        # override is the top strategy (section 5, 8.4): a human-set name wins
        forced = _override_name(ref, row, vocab)
        if forced:
            looked = ref.attributes.resolve_id(vocab, forced)
            setattr(row, f"{vocab}_id", looked[1] if looked else None)
            setattr(row, f"{vocab}_name", forced)
            setattr(row, f"{vocab}_confidence", _confidence_name(Confidence.high))
            setattr(row, f"{vocab}_method", "override")
            continue
        raw_value = getattr(row, f"raw_{vocab}", "") or ""
        first, is_multi = _split_multi(raw_value)
        resolution = resolvers.resolvers[vocab].resolve(first)

        canonical = resolution.value
        attr_id = None
        if canonical is not None:
            looked = ref.attributes.resolve_id(vocab, canonical)
            attr_id = looked[1] if looked else None

        setattr(row, f"{vocab}_id", attr_id)
        setattr(row, f"{vocab}_name", canonical)
        setattr(row, f"{vocab}_confidence", _confidence_name(resolution.confidence))
        setattr(row, f"{vocab}_method", resolution.method)

        if is_multi:
            row.add_flag(
                ReviewFlag(
                    field=vocab,
                    code=FlagCode.multi_value,
                    raw_value=raw_value,
                    best_guess=canonical,
                    confidence=Confidence.low,
                    method="multi_split",
                    src_url=row.src_url,
                )
            )
        # synonym_none is a clean resolution to "no value" (e.g. finish Other),
        # not an unresolved error, so it does not flag.
        if attr_id is None and resolution.method not in ("synonym_none", "empty"):
            row.add_flag(
                ReviewFlag(
                    field=vocab,
                    code=FlagCode.attr_unresolved,
                    raw_value=raw_value,
                    best_guess=resolution.evidence.get("best") if resolution.evidence else None,
                    confidence=resolution.confidence,
                    method=resolution.method,
                    src_url=row.src_url,
                )
            )

    # Name-over-tag type: a variety NAME with an explicit, valid stone-type word is more reliable
    # than the supplier's category tag, which is often wrong ('Azul White Quartzite' tagged Onyx,
    # 'Grey Basalt' tagged Granite). When the name carries exactly ONE valid type word that differs
    # from the resolved type, the NAME wins -- so a mis-tagged variety is corrected here in the
    # cleaning flow, never minted or imaged under the wrong type.
    name_type = explicit_type_word(row.variety_match_key or row.raw_name or "")
    if name_type and name_type.casefold() != (row.type_name or "").casefold():
        looked = ref.attributes.resolve_id("type", name_type)
        if looked:
            row.type_name, row.type_id = name_type, looked[1]
            row.type_confidence = _confidence_name(Confidence.high)
            row.type_method = "name_explicit"

    # Blocks are sold raw/unfinished, so sources rarely give a finish ('' or 'Other');
    # that would null finish_id and reject the row at validate (finish is required).
    # Default a block to 'Raw' — the standard block finish, in attributes.csv and the
    # block backbone. Check raw_format too: normalize runs BEFORE format_resolve, so
    # format_value isn't set yet -- the source format tag is what's available here.
    if (row.format_value or row.raw_format or "").strip().lower() == "block" and not row.finish_id:
        looked = ref.attributes.resolve_id("finish", "Raw")
        if looked:
            row.finish_name, row.finish_id = "Raw", looked[1]
            row.finish_confidence = _confidence_name(Confidence.low)
            row.finish_method = "block_default_raw"


def run(rows: list[CanonicalRow], ref: ReferenceData) -> AttributeResolvers:
    resolvers = AttributeResolvers.build(ref)
    for row in rows:
        normalize_row(row, resolvers, ref)
    log.info("normalize done", extra={"extra_fields": {"rows": len(rows)}})
    return resolvers
