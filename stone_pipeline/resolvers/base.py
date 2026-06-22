"""The Field Resolver: the generic mechanism (section 5).

Every derived or matched field is produced by a Resolver, an ordered list of
Strategies. Strategies are ordered by trust, not just availability: a high-trust
direct field beats a low-trust derivation even if both fire. A Resolver never
invents; if no strategy reaches the accept floor, the field is null (or a flagged
best-guess where the field config permits review emission) and a structured
review flag is added.

Manual overrides (M9) are the highest-priority Strategy for every resolver, so a
human value always wins. Adding a new field, or a new way to fill an existing
one, is adding a Strategy, not touching stage code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from stone_pipeline.config.settings import Confidence
from stone_pipeline.core.schema import CanonicalRow, FlagCode, Resolution, ReviewFlag

# A Strategy is a pure function (row, refs) -> Resolution | None.
StrategyFn = Callable[[CanonicalRow, Any], Optional[Resolution]]


@dataclass
class Strategy:
    name: str
    fn: StrategyFn

    def try_resolve(self, row: CanonicalRow, refs: Any) -> Optional[Resolution]:
        result = self.fn(row, refs)
        if result is not None and not result.method:
            result.method = self.name
        return result


@dataclass
class ResolvedField:
    value: Any
    resolution: Resolution
    flag: Optional[ReviewFlag] = None


@dataclass
class FieldResolver:
    field_name: str
    strategies: list[Strategy]
    accept: Confidence = Confidence.medium
    # whether a below-floor best-guess may still be written (for review emission)
    allow_review_value: bool = False
    flag_code: FlagCode = FlagCode.attr_unresolved

    def resolve(self, row: CanonicalRow, refs: Any) -> ResolvedField:
        results: list[Resolution] = []
        for strategy in self.strategies:
            res = strategy.try_resolve(row, refs)
            if res is not None and res.value is not None:
                results.append(res)
                # short-circuit on a trusted-enough hit (strategies are trust-ordered)
                if res.confidence >= self.accept:
                    break
        best = max(results, key=lambda r: r.confidence) if results else Resolution(
            value=None, confidence=Confidence.none, method="no_strategy"
        )
        if best.value is not None and best.confidence >= self.accept:
            return ResolvedField(value=best.value, resolution=best, flag=None)
        # below floor: never write silently
        value = best.value if (self.allow_review_value and best.value is not None) else None
        flag = ReviewFlag(
            field=self.field_name,
            code=self.flag_code,
            raw_value=str(best.evidence.get("raw")) if best.evidence.get("raw") else None,
            best_guess=str(best.value) if best.value is not None else None,
            confidence=best.confidence,
            method=best.method,
            src_url=row.src_url,
        )
        return ResolvedField(value=value, resolution=best, flag=flag)


def override_strategy(field_name: str) -> Strategy:
    """The override Strategy is the top of every Resolver (section 5, 8.4). It
    reads state/manual_overrides keyed by (src_site, surrogate_key); the actual
    override store is wired in M9, so for now it always returns None (no override)."""

    def _fn(row: CanonicalRow, refs: Any) -> Optional[Resolution]:
        overrides = getattr(refs, "overrides", None)
        if not overrides:
            return None
        key = (row.src_site, row.surrogate_key)
        value = overrides.get(key, {}).get(field_name)
        if value is None:
            return None
        return Resolution(value=value, confidence=Confidence.high, method="override")

    return Strategy(name=f"override_{field_name}", fn=_fn)
