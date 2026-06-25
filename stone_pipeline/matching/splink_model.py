"""Splink residual linkage tier (section 5A.2 tier 7).

Probabilistic record linkage on the residual only: the small set of names that
the deterministic and fuzzy tiers could not resolve, blocked by type and colour,
with EM-weighted multi-field comparison. Skip when the residual is too small to
calibrate and route those to review instead.

Splink is a heavy optional dependency and is config-gated. This module exposes
the interface and a calibration guard; the model is imported lazily so the
package runs without splink installed. When the dependency or a calibratable
residual is absent, resolve_residual returns no matches and the rows stay in the
review band, exactly as the plan specifies.
"""

from __future__ import annotations

from dataclasses import dataclass

from stone_pipeline.core import logfmt

log = logfmt.get_logger("splink")

# minimum residual size to calibrate an EM model (below this, route to review)
MIN_RESIDUAL_FOR_CALIBRATION = 30


@dataclass
class SplinkSuggestion:
    query: str
    cid: str
    score: float  # 0..100


class SplinkResolver:
    def __init__(self, candidates: list[tuple[str, str, str, str]]):
        # candidates: (cid, name, block_type, block_color)
        self.candidates = candidates
        self._available = self._check_available()

    @staticmethod
    def _check_available() -> bool:
        try:
            import splink  # noqa: F401  (optional)

            return True
        except Exception:
            return False

    def resolve_residual(self, residual: list[str]) -> list[SplinkSuggestion]:
        if not self._available:
            log.info("splink unavailable; residual routed to review")
            return []
        if len(residual) < MIN_RESIDUAL_FOR_CALIBRATION:
            log.info("residual too small to calibrate; routed to review",
                     extra={"extra_fields": {"residual": len(residual)}})
            return []
        # A full splink linkage model would be built and trained here on the
        # blocked residual. Left as the documented integration point; until it is
        # wired, the residual stays in review (never auto-accepted), which is the
        # safe default the plan mandates.
        log.info("splink model integration point reached (not yet wired)",
                 extra={"extra_fields": {"residual": len(residual)}})
        return []
