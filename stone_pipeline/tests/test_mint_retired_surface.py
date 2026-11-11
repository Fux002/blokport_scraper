"""GAP G1 (protected identity core): a confirmed mint whose deterministic Key lands on a RETIRED variety
must SURFACE an un-retire decision, never silently no-op (Window A: the retired Key is still in the export,
so the existing-Key guard would drop it) nor leak into the update delta (Window B: the retired Key was
dropped from the export). A variety has ONE Key whatever its categories. Removing the retirement (un-retire)
lets the variety mint normally again. Confined to curate mint-surfacing; type
authority / matching / reconcile are untouched.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.config.decisions_model import Decisions
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate, decisions
from stone_pipeline.stages.curate import ImportFile


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _row() -> CanonicalRow:
    """A type-less 'Retired Stone' scrape surfaced as a missing_variation gap (no existing owner)."""
    row = CanonicalRow(src_site="varsha", surrogate_key="r1", variety_match_key="Retired Stone", raw_type="")
    row.add_gap(TreeGap(src_site="varsha", surrogate_key="r1", raw_name="Retired Stone",
                        gap_kind=GapKind.missing_variation, nearest_existing=""))
    return row


def _seed(monkeypatch, tmp_path, existing: ImportFile, retired_ref: set):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))     # isolate durable decisions
    monkeypatch.setattr(curate, "load_existing", lambda: existing)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    monkeypatch.setattr(decisions, "load_decisions",           # a confirmed mint, as Granite
                        lambda: Decisions.from_legacy({("", "retired stone"): {"action": "mint", "seed_type": "Granite"}}))
    monkeypatch.setattr(decisions, "load_retired", lambda: set(retired_ref))   # mutable: baseline empty, then set


def _minted(res) -> list[str]:
    return [r["Key"] for r in res.new_variants]


def _retired_holds(res) -> list[dict]:
    return [p for p in res.pending_confirm if "RETIRED" in (p.get("reason") or "")]


def test_confirmed_non_retired_new_variety_mints_and_un_retire_restores(ref, tmp_path, monkeypatch):
    # baseline + the un-retire-restored case: with no retirement, a confirmed new variety mints and no
    # un-retire hold is surfaced. (Removing a Key from the retired set is exactly this state.)
    _seed(monkeypatch, tmp_path, ImportFile(path=None), set())
    res = curate.build_curation([_row()], ref)
    assert _minted(res), "a confirmed, non-retired new variety must mint"
    assert not _retired_holds(res)


def test_window_a_retired_key_present_in_export_surfaces_not_silent_skip(ref, tmp_path, monkeypatch):
    existing = ImportFile(path=None)
    retired: set = set()
    _seed(monkeypatch, tmp_path, existing, retired)
    base = curate.build_curation([_row()], ref)            # baseline: capture the would-be Key
    [key] = _minted(base)
    # Window A: put the variety in the export so its Key is an existing one (the guard below would otherwise
    # SILENTLY skip it) AND retire it. The fix must surface it, not no-op.
    v = {"Key": key, "Name": "Retired Stone", "Type": "Granite", "Aliases": "", "type": "granite"}
    existing.varieties.append(v)
    existing.by_name_type[("retired stone", "granite")] = v
    existing.by_name["retired stone"] = v
    retired.add(key)
    res = curate.build_curation([_row()], ref)
    assert not _minted(res), "a retired variety must not be minted"
    assert _retired_holds(res), "a retired-key mint must surface an un-retire decision, not silently no-op"


def test_window_b_retired_key_absent_from_export_surfaces_not_delta_leak(ref, tmp_path, monkeypatch):
    retired: set = set()                # variety NOT in the export: its Key is no existing one (the leak window)
    _seed(monkeypatch, tmp_path, ImportFile(path=None), retired)
    base = curate.build_curation([_row()], ref)
    keys = _minted(base)
    assert keys
    retired.update(keys)                # retire the would-be Key
    res = curate.build_curation([_row()], ref)
    assert not _minted(res), "a retired variety must not leak into new_variants / the update delta"
    assert _retired_holds(res)


def test_retired_surface_is_deterministic(ref, tmp_path, monkeypatch):
    retired: set = set()
    _seed(monkeypatch, tmp_path, ImportFile(path=None), retired)
    base = curate.build_curation([_row()], ref)
    retired.update(_minted(base))
    r1 = curate.build_curation([_row()], ref)
    r2 = curate.build_curation([_row()], ref)
    assert _minted(r1) == _minted(r2) == []                # idempotent: same export -> same (no) mint
    assert [p["variant"] for p in _retired_holds(r1)] == [p["variant"] for p in _retired_holds(r2)]
