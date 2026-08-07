"""Guard: the single Medusa requirement bar (gates/requirements.py) stays in lockstep with validate.py,
the sole hard-reject authority. If a hard requirement is added or removed in validate without updating the
documented registry (or vice versa), this test fails -- so the requirement set can never silently drift as
the pipeline evolves. This is a pure guard: it does not touch or change the working machinery.
"""

from __future__ import annotations

import pathlib
import re

import stone_pipeline.stages.validate as validate_mod
from stone_pipeline.gates.requirements import MEDUSA_RULES


def _validate_reject_rules() -> set[str]:
    """Every RejectReason rule literal validate.py can emit (the actual hard bar)."""
    src = pathlib.Path(validate_mod.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'rule="([a-z0-9_]+)"', src))


def test_medusa_requirement_registry_matches_validate_rules_exactly():
    actual = _validate_reject_rules()
    assert actual, "found no rule= literals in validate.py -- the scan or the file moved"

    missing = actual - MEDUSA_RULES
    extra = MEDUSA_RULES - actual
    assert not missing, (
        "validate.py rejects with hard rule(s) absent from the Medusa requirement registry "
        f"(gates/requirements.py): {sorted(missing)}. A hard requirement was added without documenting it -- "
        "add a Requirement entry so the single Medusa bar stays complete.")
    assert not extra, (
        "the Medusa requirement registry lists rule(s) validate.py no longer emits: "
        f"{sorted(extra)}. A requirement was removed/renamed in validate -- update the registry to match.")


def test_no_hard_rejects_hide_in_the_gates():
    """The registry pins ONLY validate because validate is the SOLE hard authority (#155). Guard that: no
    ModuleContract declares a HARD required_fields/invariant, so a hard reject cannot appear outside validate
    (and thus outside the registry) unnoticed."""
    from stone_pipeline.gates import definitions as gate_defs
    from stone_pipeline.gates.contract import HARD
    from stone_pipeline.reference import loaders

    ref = loaders.load_all()
    contracts = [gate_defs.INGEST, gate_defs.clean_contract(ref), gate_defs.PROCESS]
    hard = [(c.module, inv.name) for c in contracts for inv in c.all_invariants() if inv.severity == HARD]
    assert not hard, f"a gate declares a HARD invariant, so a hard reject lives outside validate/the registry: {hard}"
