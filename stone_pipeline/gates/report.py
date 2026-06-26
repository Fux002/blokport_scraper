"""Shared gate status vocabulary and report shape.

The OK/DEGRADED/FAILED ladder and its ranking are the same the health gate has
always used; they live here so health and every module gate share one definition
instead of duplicating it (health re-exports these names).
"""

from __future__ import annotations

from dataclasses import dataclass, field

OK = "OK"
DEGRADED = "DEGRADED"
FAILED = "FAILED"
RANK = {OK: 0, DEGRADED: 1, FAILED: 2}


def worse(current: str, candidate: str) -> str:
    """The more severe of two statuses (used to monotonically escalate)."""
    return candidate if RANK[candidate] > RANK[current] else current


@dataclass
class GateReport:
    """The outcome of applying one ModuleContract to a batch of rows."""

    module: str
    status: str = OK
    rows_in: int = 0
    rows_rejected: int = 0  # unique rows this gate newly hard-rejected
    rows_flagged: int = 0  # unique rows this gate newly soft-flagged
    violations: dict[str, int] = field(default_factory=dict)  # invariant name -> rows violating it
    messages: list[str] = field(default_factory=list)

    def escalate(self, status: str) -> None:
        self.status = worse(self.status, status)

    def summary(self) -> str:
        """One-line, human-readable status for the run log / SYNC_STEPS."""
        parts = [f"{self.module}: {self.status} ({self.rows_in} rows"]
        if self.rows_rejected:
            parts.append(f", {self.rows_rejected} rejected")
        if self.rows_flagged:
            parts.append(f", {self.rows_flagged} flagged")
        parts.append(")")
        if self.violations:
            top = ", ".join(f"{k}={v}" for k, v in sorted(self.violations.items()))
            parts.append(f" [{top}]")
        return "".join(parts)
