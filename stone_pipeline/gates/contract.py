"""Declarative module-boundary contracts.

A ModuleContract is plain data (like config/contracts.py's SourceContract): the
fields and invariants a module guarantees on its output. The runner applies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from stone_pipeline.core.schema import CanonicalRow, FlagCode

# Invariant severity:
HARD = "hard"  # violation -> RejectReason; the row will not emit
SOFT = "soft"  # violation -> ReviewFlag (if flag_code set) or report-only (if not); the row still emits


@dataclass
class Invariant:
    """One named, row-level check a module's output must satisfy.

    `check(row)` returns True when the row SATISFIES the invariant. `name` is the
    stable id surfaced in the GateReport and used as the RejectReason rule. For a
    SOFT invariant, set `flag_code` to stamp a ReviewFlag; leave it None to make
    the invariant report-only (it contributes to the batch status and the
    "what's missing" diagnostics without mutating the row).
    """

    name: str
    check: Callable[[CanonicalRow], bool]
    severity: str = HARD
    flag_code: Optional[FlagCode] = None
    field: str = ""  # the row field this concerns (surfaced on the flag / report)


def _nonempty(field_name: str) -> Callable[[CanonicalRow], bool]:
    def _check(row: CanonicalRow) -> bool:
        value = getattr(row, field_name, None)
        if isinstance(value, str):
            return bool(value.strip())
        return value is not None and value != [] and value != ""

    return _check


@dataclass
class ModuleContract:
    """The output contract enforced at the seam after a logical module."""

    module: str
    # canonical fields that must be non-empty after this module (hard). Shorthand for a
    # required-field invariant; the runner expands each into a HARD non-empty Invariant.
    required_fields: tuple[str, ...] = ()
    invariants: tuple[Invariant, ...] = ()
    # batch-level: if the fraction of rows violating ANY single invariant reaches these,
    # the module's whole output is suspect -- a systemic bug, not unlucky rows.
    batch_warn_fraction: float = 0.5
    batch_fail_fraction: float = 0.9

    def all_invariants(self) -> list[Invariant]:
        """required_fields expanded into HARD non-empty invariants, plus the explicit ones."""
        expanded = [
            Invariant(name=f"{self.module}_missing_{f}", check=_nonempty(f), severity=HARD, field=f)
            for f in self.required_fields
        ]
        return [*expanded, *self.invariants]
