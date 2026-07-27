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
