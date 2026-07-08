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
from stone_pipeline.config.settings import LAST_RESORT_FINISH, LAST_RESORT_QUALITY, SETTINGS, Confidence
from stone_pipeline.core import logfmt
from stone_pipeline.core.schema import CanonicalRow, FlagCode, ReviewFlag
from stone_pipeline.matching.engine import VocabResolver
from stone_pipeline.reference.loaders import ReferenceData

log = logfmt.get_logger("normalize")

# vocab -> (raw field, code, multi-value allowed)
VOCAB_FIELDS = ("type", "color", "finish", "quality")
# Only unambiguous separators (pipe / slash / comma). ' and ' is NOT a separator: 'Black and Gold'
# is a single colour descriptor, and splitting it silently dropped 'Gold' and mis-flagged multi_value.
# A genuine 'X and Y' now stays one value -> it simply fails to resolve and is flagged for review.
_MULTI_SPLIT = re.compile(r"\s*[|/,]\s*")


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
        if resolution.value is None and not is_multi and raw_value:
            # The whole value didn't resolve. A compound joined by 'and'/'&' ('White and Grey') is tried
            # WHOLE first (so a real single descriptor / synonym wins), then falls back to the first
            # RESOLVABLE conjunct -- flagged multi_value -- so the product still ships instead of being
            # hard-rejected for a null id. (A genuine single 'Black and Gold' that resolves whole never
            # reaches here.)
            conjuncts = re.split(r"\s+(?:and|&)\s+", raw_value, flags=re.IGNORECASE)
            if len(conjuncts) > 1:
                for cand in conjuncts:
                    alt = resolvers.resolvers[vocab].resolve(cand.strip())
                    if alt.value is not None:
                        resolution, first, is_multi = alt, cand.strip(), True
                        break

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
            # looked is (canonical_name, id) -- use the canonical name, not the raw
            # token, so a miscased source word ('QUARTZITE') becomes 'Quartzite'.
            row.type_name, row.type_id = looked
            row.type_confidence = _confidence_name(Confidence.high)
            row.type_method = "name_explicit"

    # Blocks are sold raw/unfinished, so sources rarely give a finish ('' or 'Other');
    # that would null finish_id and reject the row at validate (finish is required).
    # Default a block to 'Raw' — the standard block finish, in attributes.csv and the
    # block backbone. Check raw_format too: normalize runs BEFORE format_resolve, so
    # format_value isn't set yet -- the source format tag is what's available here.
    fmt = (row.format_value or row.raw_format or "").strip().lower()
    if fmt == "block" and not row.finish_id:
        looked = ref.attributes.resolve_id("finish", "Raw")
        if looked:
            row.finish_name, row.finish_id = "Raw", looked[1]
            row.finish_confidence = _confidence_name(Confidence.low)
            row.finish_method = "block_default_raw"

    # Last-resort attribute defaults (config policy, section: never drop a product for a resolvable-
    # attribute gap). When a REQUIRED attribute could not be resolved from the source, apply the
    # configured default (settings.LAST_RESORT_*) as the FINAL rung, and ALWAYS flag it for correction,
    # instead of nulling the id and rejecting the product at validate. Deliberate, changeable, visible.
    # Colour is NOT defaulted here: it is inherited from the matched variety / classified from the
    # texture ('Natural' floor), so a colour default here would mask that better value.
    if not row.finish_id:                                   # non-block finish (block handled above)
        default_finish = LAST_RESORT_FINISH.get(fmt) or LAST_RESORT_FINISH.get("slab")
        looked = ref.attributes.resolve_id("finish", default_finish) if default_finish else None
        if looked:
            row.finish_name, row.finish_id = looked
            row.finish_confidence = _confidence_name(Confidence.low)
            row.finish_method = "last_resort_default"
            row.add_flag(ReviewFlag(field="finish", code=FlagCode.attr_last_resort, best_guess=looked[0],
                                    confidence=Confidence.low, method="last_resort_default", src_url=row.src_url))
    if not row.quality_id:
        looked = ref.attributes.resolve_id("quality", LAST_RESORT_QUALITY)
        if looked:
            row.quality_name, row.quality_id = looked
            row.quality_confidence = _confidence_name(Confidence.low)
            row.quality_method = "last_resort_default"
            row.add_flag(ReviewFlag(field="quality", code=FlagCode.attr_last_resort, best_guess=looked[0],
                                    confidence=Confidence.low, method="last_resort_default", src_url=row.src_url))


def run(rows: list[CanonicalRow], ref: ReferenceData) -> AttributeResolvers:
    resolvers = AttributeResolvers.build(ref)
    for row in rows:
        normalize_row(row, resolvers, ref)
    log.info("normalize done", extra={"extra_fields": {"rows": len(rows)}})
    return resolvers
