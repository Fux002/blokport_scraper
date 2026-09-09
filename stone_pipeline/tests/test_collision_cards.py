"""A trade-name COLLISION surfaces as ONE review card and is never resolved by an arbitrary pick.

'Amazon Green Granite' is an alias of three distinct granites, none NAMED that. Whether the matcher flagged
the collision (its verdict names the owners on the gap) or curate finds several same-type owners of the
cleaned surface itself, the row must hold with the owners listed: no alias onto the first owner, no mint.
The operator's answer may be vendor-scoped ('for marenostone it is Golden Lightning'). Hermetic.
"""
from __future__ import annotations

from stone_pipeline.core.schema import CanonicalRow, GapKind, TreeGap
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate
from stone_pipeline.stages.curate import ImportFile

OWNERS = (("slab_granite_amazon_blue_A", "Amazon Blue"), ("slab_granite_amazonia_B", "Amazonia"),
          ("slab_granite_verde_ubatuba_C", "Verde Ubatuba"))


def _imports() -> dict[str, ImportFile]:
    """Three SAME-TYPE granites that all list 'Amazon Green Granite' as an alias; none is named that."""
    branches = {}
    for b in ("slab", "block", "tile"):
        imp = ImportFile(branch=b, path=None)
        if b == "slab":
            for key, nm in OWNERS:
                # both the full spelling and the type-stripped one the cleaner produces ('Amazon Green')
                v = {"Key": key, "Name": nm, "Image": "", "Aliases": "Amazon Green Granite|Amazon Green", "Volume": "",
                     "type": curate.proj.norm(loaders.type_slug_from_key(key))}
                imp.varieties.append(v)
                imp.by_name_type[(curate.proj.norm(nm), v["type"])] = v
                imp.by_name[curate.proj.norm(nm)] = v
        branches[b] = imp
    return branches


def _row(method: str, nearest: str | None) -> CanonicalRow:
    g = TreeGap(src_site="marenostone", surrogate_key="1", raw_name="Amazon Green Granite Slab",
                normalized_name="amazon green granite", gap_kind=GapKind.missing_variation,
                nearest_existing=nearest)
    return CanonicalRow(src_site="marenostone", surrogate_key="1", variety_match_key="Amazon Green Granite",
                        raw_type="Granite", variation_method=method, tree_gaps=[g])


def _minted(res) -> bool:
    return any(res.new_variants.get(b) for b in res.new_variants)


def _card(res):
    return next((p for p in res.pending_confirm if p["variant"].lower().startswith("amazon green")), None)


def test_matcher_collision_holds_with_the_owners_it_named(monkeypatch):
    monkeypatch.setattr(curate, "load_existing", lambda b: _imports()[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()
    res = curate.build_curation([_row("exact_collision", "Amazon Blue, Amazonia, Verde Ubatuba")], ref)
    assert not res.alias_additions["slab"], "must not alias onto the first same-type owner"
    assert not _minted(res), "must not mint a duplicate"
    card = _card(res)
    assert card and "Matches several existing varieties" in card["reason"]
    assert all(n in card["reason"] for n in ("Amazon Blue", "Amazonia", "Verde Ubatuba"))
    assert card["stone_type"] == "Granite" and card["src"] == "marenostone"


def test_a_typed_gap_with_several_same_type_owners_and_no_canonical_also_holds(monkeypatch):
    # the pre-existing curate path (no matcher verdict): the cleaned surface is owned by three same-type
    # varieties, none named that -> the same card, not sorted(owners)[0]
    monkeypatch.setattr(curate, "load_existing", lambda b: _imports()[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()
    res = curate.build_curation([_row("no_candidate", None)], ref)
    assert not res.alias_additions["slab"] and not _minted(res)
    card = _card(res)
    assert card and "Matches several existing varieties" in card["reason"] and "Amazonia" in card["reason"]


def test_a_single_same_type_owner_still_aliases(monkeypatch):
    # the unambiguous case must not regress into a hold: one same-type owner -> alias onto it
    imports = _imports()
    imports["slab"].varieties = imports["slab"].varieties[:1]
    imports["slab"].varieties[0]["Aliases"] = "Amazon Green"   # the full spelling is NEW -> an alias addition
    imports["slab"].by_name_type = {k: v for k, v in imports["slab"].by_name_type.items() if v["Name"] == "Amazon Blue"}
    imports["slab"].by_name = {k: v for k, v in imports["slab"].by_name.items() if v["Name"] == "Amazon Blue"}
    monkeypatch.setattr(curate, "load_existing", lambda b: imports[b])
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()
    res = curate.build_curation([_row("no_candidate", None)], ref)
    assert [a["Key"] for a in res.alias_additions["slab"]] == ["slab_granite_amazon_blue_A"]
    assert _card(res) is None




def test_a_statement_binds_the_spelling_for_that_vendor_only(tmp_path, monkeypatch):
    from stone_pipeline.config import decisions_store, server, varieties
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: (n, t) == ("Golden Lightning", "Granite"))
    monkeypatch.setattr(varieties, "alias_target", lambda n, t: None)
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Amazon Green Granite",
                                  "name": "Golden Lightning", "type": "Granite"})
    assert code == 200 and body["result"] == "bound"
    assert decisions_store.scoped_aliases() == {("marenostone", "amazon green granite"): ("Golden Lightning", "Granite")}
    assert decisions_store.variety_actions() == {}          # scoped: NOT a global decision
