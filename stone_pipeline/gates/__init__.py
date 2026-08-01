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
OK -> DEGRADED -> FAILED -- the same status ladder the health gate uses.

What a FAILED status does depends on WHICH gate (they are not uniform, by design):
  - the INGEST gate aborts the run before emit (raise SystemExit in run.run_source):
    a batch that fails the input contract has nothing trustworthy to derive from.
  - the CLEAN and PROCESS gates do NOT abort. They are advisory at the batch level:
    per-row hard/soft violations already route each row (reject / review) through
    validate, which is the authority on what emits, so the surviving rows still
    emit -- to the REVIEW queue for a review-mode source. The FAILED batch status is
    recorded on the manifest and BLOCKS an `auto`-mode source from auto-loading
    (run.auto_sources_blocked), so a systemic regression never auto-publishes; a
    review-mode source is already quarantined. Aborting here would instead deny the
    operator the very rows they need to see in review.
Every gate's FAILED status is always recorded on the manifest regardless.
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
