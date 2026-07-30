"""Resolving a scrape to an EXISTING variety must never mint a duplicate under the scraped spelling.

The bug (found in interactive testing, the 'Monalisa' case): a product-backed scrape whose name is an ALIAS
of a differently-named existing variety ('Monalisa' -> existing 'Mona Lisa' Granite) resolved correctly, but
`_alias_and_backfill` then queued a backfill mint under the SCRAPED spelling. Its Key core
(granite_monalisa) does not match the owner's (granite_mona_lisa), so the existing-cores dedup guard could
not skip it and a duplicate variety was minted in every branch -- and it shipped (emit never merges distinct
names). The fix: backfill the missing cross-branch sibling under the OWNER's canonical name, so the guard
skips branches where the owner already lives and only a genuinely-missing branch is filled, as a true sibling.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate, decisions
from stone_pipeline.stages.curate import ImportFile


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _imports_with_mona_lisa(branches: set[str]) -> dict[str, ImportFile]:
    """Existing catalog carrying 'Mona Lisa' Granite (alias 'Monalisa') in the given branches only."""
    out = {}
    for b in ("slab", "block", "tile"):
        imp = ImportFile(branch=b, path=None, present=True)
        if b in branches:
            v = {"Key": f"{b}_granite_mona_lisa_x", "Name": "Mona Lisa", "Image": "",
                 "Aliases": "Monalisa", "Volume": "", "type": "granite"}
            imp.varieties.append(v)
            imp.by_name_type[("mona lisa", "granite")] = v
            imp.by_name["mona lisa"] = v
        out[b] = imp
    return out


def _monalisa_row() -> CanonicalRow:
    # a type-less 'Monalisa' scrape surfaced as a missing_variation gap (product-backed)
    row = CanonicalRow(src_site="varsha", surrogate_key="m1", variety_match_key="Monalisa", raw_type="")
    row.add_gap(TreeGap(src_site="varsha", surrogate_key="m1", raw_name="Monalisa",
                        gap_kind=GapKind.missing_variation, nearest_existing="Mona Lisa"))
    return row


def _seed_granite(monkeypatch, imports):
    monkeypatch.setattr(curate, "load_existing", lambda b: imports[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    # the operator assigned Granite to the type-less multi-surface name
    monkeypatch.setattr(decisions, "load_variety_seed_types", lambda: {"monalisa": "Granite"})


def test_resolve_to_existing_alias_never_mints_the_scraped_spelling(ref, monkeypatch):
    # Mona Lisa Granite exists in every branch -> resolving 'Monalisa'+Granite must mint NOTHING.
    _seed_granite(monkeypatch, _imports_with_mona_lisa({"slab", "block", "tile"}))
    res = curate.build_curation([_monalisa_row()], ref)

    minted = [r["Key"] for b in ("slab", "block", "tile") for r in res.new_variants[b]]
    assert not any("monalisa" in k for k in minted), f"minted a duplicate under the scraped spelling: {minted}"
    assert not any("mona_lisa" in k for k in minted), f"re-minted the existing owner (guard failed): {minted}"


def test_resolve_to_existing_mints_nothing_in_the_uniform_union_model(ref, monkeypatch):
    # Under the uniform UNION model, a variety belongs to the stone not a format: curate's existing_cores is
    # the union of cores across all categories, so a variety present in slab already 'exists' in block/tile
    # -- there is no 'missing branch' to backfill; the emit's union gap-fill provides the other categories.
    # Resolving 'Monalisa' onto the existing Mona Lisa Granite must therefore mint NOTHING in any branch --
    # not the scraped spelling (no duplicate), and not a spurious owner-named backfill (union-fill handles it).
    _seed_granite(monkeypatch, _imports_with_mona_lisa({"slab"}))
    res = curate.build_curation([_monalisa_row()], ref)

    minted = [r["Key"] for b in ("slab", "block", "tile") for r in res.new_variants[b]]
    assert not any("monalisa" in k for k in minted), f"duplicate under scraped spelling: {minted}"
    assert not any("mona_lisa" in k for k in minted), f"spurious backfill; union-fill provides block/tile: {minted}"
