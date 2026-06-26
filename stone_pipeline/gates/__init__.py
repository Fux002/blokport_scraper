"""Per-module boundary gates.

Each logical pipeline module (ingest, clean, process, images, upgrade) ends in a
gate that enforces the module's output *contract* -- the fields and invariants it
guarantees -- before the next module runs. A gate reuses the existing row
machinery rather than inventing new routing: a hard violation stamps a
RejectReason (the row will not emit, via CanonicalRow.is_emittable), a soft
violation stamps a ReviewFlag (the row emits but is queued for review), and a
report-only invariant contributes only to the batch status + "what's missing"
diagnostics. When a single invariant is violated by a large fraction of the
batch, that is a systemic bug (not unlucky rows), so the gate escalates
OK -> DEGRADED -> FAILED -- the same status ladder the health gate uses, and a
FAILED gate aborts the run before emit.
"""

from stone_pipeline.gates.contract import HARD, SOFT, Invariant, ModuleContract
from stone_pipeline.gates.report import DEGRADED, FAILED, OK, GateReport
from stone_pipeline.gates.runner import apply

__all__ = [
    "apply",
    "GateReport",
    "ModuleContract",
    "Invariant",
    "HARD",
    "SOFT",
    "OK",
    "DEGRADED",
    "FAILED",
]
