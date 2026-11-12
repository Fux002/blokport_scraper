"""Resolving a scrape to an EXISTING variety must never mint a duplicate under the scraped spelling.

The bug (found in interactive testing, the 'Monalisa' case): a product-backed scrape whose name is an ALIAS
of a differently-named existing variety ('Monalisa' -> existing 'Mona Lisa' Granite) resolved correctly, but
a backfill then queued a mint under the SCRAPED spelling, and a duplicate variety shipped (emit never merges
distinct names). A variety is now ONE record per (type, name) (phase 1), so there is no per-category sibling to
backfill: resolving onto the owner records the spelling as the owner's alias and mints nothing.
"""

from __future__ import annotations

import pytest

from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.phase1.variety_key import new_key
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate
from stone_pipeline.stages.curate import ImportFile


@pytest.fixture(scope="module")
def ref():
    return loaders.load_all()


def _existing_mona_lisa() -> ImportFile:
    """Existing catalog carrying 'Mona Lisa' Granite (alias 'Monalisa')."""
    imp = ImportFile(path=None)
    v = {"Key": new_key("Granite", "Mona Lisa"), "Name": "Mona Lisa", "Type": "Granite", "Aliases": "Monalisa",
         "type": "granite"}
    imp.varieties.append(v)
    imp.by_name_type[("mona lisa", "granite")] = v
    imp.by_name["mona lisa"] = v
    return imp


def _monalisa_row() -> CanonicalRow:
    # a Granite-typed 'Monalisa' scrape: the type is the scrape's own (a type can only otherwise come from a
    # MINT statement, which would mint rather than resolve), and 'Monalisa' is an alias of the existing owner
    row = CanonicalRow(src_site="varsha", surrogate_key="m1", variety_match_key="Monalisa", raw_type="Granite")
    row.add_gap(TreeGap(src_site="varsha", surrogate_key="m1", raw_name="Monalisa",
                        gap_kind=GapKind.missing_variation, nearest_existing="Mona Lisa"))
    return row


def test_resolve_to_existing_alias_never_mints_the_scraped_spelling(ref, monkeypatch):
    monkeypatch.setattr(curate, "load_existing", _existing_mona_lisa)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    res = curate.build_curation([_monalisa_row()], ref)
    minted = [r["Key"] for r in res.new_variants]
    assert not any("monalisa" in k for k in minted), f"minted a duplicate under the scraped spelling: {minted}"
    assert not any("mona_lisa" in k for k in minted), f"re-minted the existing owner: {minted}"
