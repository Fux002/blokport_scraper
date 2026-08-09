"""GAP G1 (protected identity core): a confirmed mint whose deterministic Key lands on a RETIRED variety
must SURFACE an un-retire decision, never silently no-op (Window A: the retired Key is still in the export,
so the branch-dedup guard would drop it) nor leak into the update delta (Window B: the retired Key was
dropped from the export). Retirement is per-variety: ANY retired fan-out Key holds ALL formats. Removing the
retirement (un-retire) lets the variety mint normally again. Confined to curate mint-surfacing; type
authority / matching / reconcile are untouched.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate, decisions
from stone_pipeline.stages.curate import ImportFile

_BRANCHES = ("slab", "block", "tile")


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _row() -> CanonicalRow:
    """A type-less 'Retired Stone' scrape surfaced as a missing_variation gap (no existing owner)."""
    row = CanonicalRow(src_site="varsha", surrogate_key="r1", variety_match_key="Retired Stone", raw_type="")
    row.add_gap(TreeGap(src_site="varsha", surrogate_key="r1", raw_name="Retired Stone",
                        gap_kind=GapKind.missing_variation, nearest_existing=""))
    return row


def _empty_imports() -> dict[str, ImportFile]:
    return {b: ImportFile(branch=b, path=None, present=True) for b in _BRANCHES}


def _seed(monkeypatch, tmp_path, imports, retired_ref: set):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))     # isolate durable decisions
    monkeypatch.setattr(curate, "load_existing", lambda b: imports[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    monkeypatch.setattr(decisions, "load_variety_seed_types", lambda: {"retired stone": "Granite"})
    monkeypatch.setattr(decisions, "load_confirm_decisions", lambda: {"retired stone": "yes"})   # confirmed mint
    monkeypatch.setattr(decisions, "load_retired", lambda: set(retired_ref))   # mutable: baseline empty, then set


def _minted(res) -> list[str]:
    return [r["Key"] for b in _BRANCHES for r in res.new_variants[b]]


def _retired_holds(res) -> list[dict]:
    return [p for p in res.pending_confirm if "RETIRED" in (p.get("reason") or "")]


def test_confirmed_non_retired_new_variety_mints_and_un_retire_restores(ref, tmp_path, monkeypatch):
    # baseline + the un-retire-restored case: with no retirement, a confirmed new variety mints and no
    # un-retire hold is surfaced. (Removing a Key from the retired set is exactly this state.)
    _seed(monkeypatch, tmp_path, _empty_imports(), set())
    res = curate.build_curation([_row()], ref)
    assert _minted(res), "a confirmed, non-retired new variety must mint into the active branches"
    assert not _retired_holds(res)


def test_window_a_retired_key_present_in_export_surfaces_not_silent_skip(ref, tmp_path, monkeypatch):
    imports = _empty_imports()
    retired: set = set()
    _seed(monkeypatch, tmp_path, imports, retired)
    base = curate.build_curation([_row()], ref)            # baseline: capture the would-be Keys
    keys = {b: r["Key"] for b in _BRANCHES for r in base.new_variants[b]}
    assert keys, "sanity: the variety mints when not retired"
    # Window A: put the variety in the export so its core is in union_cores (the dedup guard below would
    # otherwise SILENTLY skip it) AND retire it. The fix must surface it, not no-op.
    for b, k in keys.items():
        v = {"Key": k, "Name": "Retired Stone", "Image": "", "Aliases": "", "Volume": "", "type": "granite"}
        imports[b].varieties.append(v)
        imports[b].by_name_type[("retired stone", "granite")] = v
        imports[b].by_name["retired stone"] = v
    retired.update(keys.values())
    res = curate.build_curation([_row()], ref)
    assert not _minted(res), "a retired variety must not be minted"
    assert _retired_holds(res), "a retired-key mint must surface an un-retire decision, not silently no-op"


def test_window_b_retired_key_absent_from_export_surfaces_not_delta_leak(ref, tmp_path, monkeypatch):
    imports = _empty_imports()          # variety NOT in the export -> union_cores lacks its core (the leak window)
    retired: set = set()
    _seed(monkeypatch, tmp_path, imports, retired)
    base = curate.build_curation([_row()], ref)
    keys = [r["Key"] for b in _BRANCHES for r in base.new_variants[b]]
    assert keys
    retired.update(keys)                # retire the would-be Keys
    res = curate.build_curation([_row()], ref)
    assert not _minted(res), "a retired variety must not leak into new_variants / the update delta"
    assert _retired_holds(res)


def test_per_variety_any_retired_branch_holds_all_formats(ref, tmp_path, monkeypatch):
    imports = _empty_imports()
    retired: set = set()
    _seed(monkeypatch, tmp_path, imports, retired)
    base = curate.build_curation([_row()], ref)
    keys = {b: r["Key"] for b in _BRANCHES for r in base.new_variants[b]}
    assert len(keys) >= 2, "need >=2 active branches to prove the per-variety hold"
    retired.add(next(iter(keys.values())))                 # retire ONE branch's Key only
    res = curate.build_curation([_row()], ref)
    assert not _minted(res), "any retired fan-out Key must hold the WHOLE variety (all formats), not just one"
    assert _retired_holds(res)


def test_retired_surface_is_deterministic(ref, tmp_path, monkeypatch):
    imports = _empty_imports()
    retired: set = set()
    _seed(monkeypatch, tmp_path, imports, retired)
    base = curate.build_curation([_row()], ref)
    retired.update(r["Key"] for b in _BRANCHES for r in base.new_variants[b])
    r1 = curate.build_curation([_row()], ref)
    r2 = curate.build_curation([_row()], ref)
    assert _minted(r1) == _minted(r2) == []                # idempotent: same export -> same (no) mint
    assert [p["variant"] for p in _retired_holds(r1)] == [p["variant"] for p in _retired_holds(r2)]
