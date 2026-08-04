"""Minting type-authority: a variety's stone TYPE (taken from its Key, the authority) governs matching and
alias routing, so a same-name variety of a different stone is never collapsed into one.

The known risk (memory: classification-type-authority-review / curate-type-aware-existing-index, PR #45):
split type authority let cross-type products ship -- a name that exists as stone A getting merged with the
same name scraped as stone B. These tests pin the fix at its two load-bearing points:
  1. load_existing indexes the existing catalog by (name, TYPE), so two same-name different-type varieties
     coexist and are addressable separately (name-only lookup is an explicit typeless fallback).
  2. A confirmed non-exact match attaches its scraped spelling to the (name, Key-TYPE) owner -- the Granite
     variety, never the same-name Marble one.
"""

from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate
from stone_pipeline.stages.curate import ImportFile

# Arabescato is single-type Marble in the real backbone; we introduce a synthetic Granite twin to model the
# same-name-different-stone collision that is the crux of the cross-type bug.
MARBLE_KEY = "slab_marble_arabescato_A"
GRANITE_KEY = "slab_granite_arabescato_B"


def _slab_imports() -> dict[str, ImportFile]:
    """A minimal existing catalog: two same-name ('Arabescato') varieties of different stones on the slab
    branch, empty block/tile. Built via the real load_existing over a stub export so the (name,type) index
    is populated by production code, not by hand."""
    branches = {}
    for b in ("slab", "block", "tile"):
        imp = ImportFile(branch=b, path=None, present=(b == "slab"))
        if b == "slab":
            # Granite first, MARBLE last, so the name-only fallback (by_name, last-wins) resolves to MARBLE.
            # The keyed path must still land on Granite -- so if line 359 ever regressed to a name-only owner,
            # the alias would wrongly hit Marble and this test would fail. Discriminating, not correct-by-luck.
            for key, name in ((GRANITE_KEY, "Arabescato"), (MARBLE_KEY, "Arabescato")):
                v = {"Key": key, "Name": name, "Image": "", "Aliases": "", "Volume": "",
                     "type": curate.proj.norm(loaders.type_slug_from_key(key))}
                imp.varieties.append(v)
                imp.by_name_type[(curate.proj.norm(name), v["type"])] = v
                imp.by_name[curate.proj.norm(name)] = v   # name-only fallback: last-wins -> Marble
        branches[b] = imp
    return branches


def test_load_existing_keys_same_name_different_type_separately(tmp_path, monkeypatch):
    exp = tmp_path / "variants_export.csv"
    with exp.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"])
        w.writeheader()
        w.writerow({"Id": "1", "Key": MARBLE_KEY, "Name": "Arabescato"})
        w.writerow({"Id": "2", "Key": GRANITE_KEY, "Name": "Arabescato"})
    monkeypatch.setattr(curate, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(export_file=exp)))

    imp = curate.load_existing("slab")
    # both stones survive under one name, addressed by (name, TYPE) -- not collapsed to a single variety
    assert imp.by_name_type[("arabescato", "marble")]["Key"] == MARBLE_KEY
    assert imp.by_name_type[("arabescato", "granite")]["Key"] == GRANITE_KEY
    assert len(imp.varieties) == 2
    # the type is taken from the Key (the authority), never guessed from the name
    assert {v["type"] for v in imp.varieties} == {"marble", "granite"}


def test_non_exact_match_aliases_onto_the_matched_type_never_the_same_name_other_type(monkeypatch):
    # A scrape fuzzy-matched to the GRANITE Arabescato (variation_key carries its type) must attach its
    # spelling to the Granite variety, never bleed onto the same-name Marble one.
    monkeypatch.setattr(curate, "load_existing", lambda b: _slab_imports()[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    row = CanonicalRow(src_site="polonine", surrogate_key="a1",
                       variety_match_key="Arabesk",           # the scraped spelling to alias
                       variation_id="v1", variation_key=GRANITE_KEY,
                       variation_name="Arabescato", variation_method="model")   # non-exact -> alias path
    result = curate.build_curation([row], ref)

    slab_aliases = result.alias_additions["slab"]
    on_granite = [a for a in slab_aliases if a["Key"] == GRANITE_KEY and "Arabesk" in (a.get("_added") or "")]
    on_marble = [a for a in slab_aliases if a["Key"] == MARBLE_KEY]
    assert on_granite, f"spelling should alias onto the Granite variety, got {slab_aliases}"
    assert not on_marble, f"spelling must NOT touch the same-name Marble variety, got {on_marble}"


def _gap_row(name: str) -> CanonicalRow:
    """A type-less scrape whose name matches an existing variety but gaps as missing_variation (the matcher
    could not pick a type). Drives curate's gap-classification loop."""
    from stone_pipeline.core.schema import GapKind, TreeGap
    g = TreeGap(src_site="polonine", surrogate_key="g1", raw_name=name,
                normalized_name=curate.proj.norm(name), gap_kind=GapKind.missing_variation)
    return CanonicalRow(src_site="polonine", surrogate_key="g1", variety_match_key=name,
                        variation_id=None, variation_key=None, variation_name=None,
                        variation_method="exact_name_ambiguous", tree_gaps=[g])


def test_alias_type_pick_resolves_a_multitype_hold_instead_of_reholding(tmp_path, monkeypatch):
    # THE alias asymmetry fix: a type-less scrape of a name that exists as SEVERAL stones holds for a type.
    # Once the operator answers that hold by picking a type ON AN ALIAS decision, variety_identity must read
    # that chosen type (as it already does for a mint's seed_type) so the row RESOLVES onto that type next
    # produce -- not re-hold forever (the Monalisa-stuck bug).
    from stone_pipeline.stages import decisions
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))   # isolate: all other decisions empty
    monkeypatch.setattr(curate, "load_existing", lambda b: _slab_imports()[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    # (a) no decision -> HOLD for a type (unchanged behaviour), never an arbitrary stone
    monkeypatch.setattr(decisions, "load_alias_types", lambda: {})
    held = curate.build_curation([_gap_row("Arabescato")], ref)
    assert any(p["variant"].lower() == "arabescato" for p in held.pending_confirm), "must hold for a type"
    assert not held.alias_additions["slab"], "must not resolve onto any stone without a decision"

    # (b) operator picked Granite on the alias -> resolves onto Granite ONLY, no hold
    monkeypatch.setattr(decisions, "load_alias_types", lambda: {"arabescato": "Granite"})
    done = curate.build_curation([_gap_row("Arabescato")], ref)
    assert not any(p["variant"].lower() == "arabescato" for p in done.pending_confirm), "must stop holding"
    on_granite = [a for a in done.alias_additions["slab"] if a["Key"] == GRANITE_KEY]
    on_marble = [a for a in done.alias_additions["slab"] if a["Key"] == MARBLE_KEY]
    assert on_granite and not on_marble, f"must resolve onto Granite only, got {done.alias_additions['slab']}"

    # (c) a NON-CANONICAL picked type is dropped -> still held (never mints a garbage-slug Key)
    monkeypatch.setattr(decisions, "load_alias_types", lambda: {"arabescato": "Wibble"})
    bad = curate.build_curation([_gap_row("Arabescato")], ref)
    assert any(p["variant"].lower() == "arabescato" for p in bad.pending_confirm), "non-canonical type -> still held"


def test_alias_family_surfaces_to_review_not_a_hidden_side_file(tmp_path, monkeypatch):
    # A type-less scrape whose surface is an ALIAS of SEVERAL different-named varieties is an uncertain
    # identity -> it must appear on the review list (pick which / mint new), never be filed away silently.
    branches = {}
    for b in ("slab", "block", "tile"):
        imp = curate.ImportFile(branch=b, path=None, present=(b == "slab"))
        if b == "slab":
            # two DIFFERENT varieties (Alpha, Beta) that both carry 'Shared Surface' as an alias
            for key, nm in (("slab_marble_alpha_1", "Alpha"), ("slab_granite_beta_2", "Beta")):
                v = {"Key": key, "Name": nm, "Image": "", "Aliases": "Shared Surface", "Volume": "",
                     "type": curate.proj.norm(loaders.type_slug_from_key(key))}
                imp.varieties.append(v)
                imp.by_name_type[(curate.proj.norm(nm), v["type"])] = v
                imp.by_name[curate.proj.norm(nm)] = v
        branches[b] = imp
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(curate, "load_existing", lambda b: branches[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    from stone_pipeline.core.schema import GapKind, TreeGap
    g = TreeGap(src_site="polonine", surrogate_key="f1", raw_name="Shared Surface",
                normalized_name="shared surface", gap_kind=GapKind.missing_variation)
    row = CanonicalRow(src_site="polonine", surrogate_key="f1", variety_match_key="Shared Surface",
                       variation_id=None, variation_key=None, variation_name=None,
                       variation_method="exact_ambiguous", tree_gaps=[g])
    result = curate.build_curation([row], ref)
    p = next((p for p in result.pending_confirm if p["variant"].lower() == "shared surface"), None)
    assert p is not None, "alias-family identity must reach the review list"
    assert "Alpha" in p["reason"] and "Beta" in p["reason"], "review must name the candidate varieties"


def test_typeless_match_on_multi_type_name_holds_never_picks_an_arbitrary_stone(monkeypatch):
    # A match that carries NO stable Key (keyless -- e.g. a forced override at a stale id, or an operator
    # alias-by-name) on a name that exists as SEVERAL stones must NOT silently attach to an arbitrary one.
    # _by_name_owner refuses the ambiguous name, so no alias is emitted on either variety (the caller HOLDs).
    monkeypatch.setattr(curate, "load_existing", lambda b: _slab_imports()[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()

    row = CanonicalRow(src_site="polonine", surrogate_key="a2",
                       variety_match_key="Arabesk",
                       variation_id="v1", variation_key=None,          # keyless -> name-only resolution
                       variation_name="Arabescato", variation_method="model")
    result = curate.build_curation([row], ref)

    touched = [a for a in result.alias_additions["slab"] if a["Key"] in (MARBLE_KEY, GRANITE_KEY)]
    assert not touched, f"multi-type name must not resolve to an arbitrary stone, got {touched}"
