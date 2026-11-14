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
from stone_pipeline.tests import _decision_views as views
from stone_pipeline.tests._existing_index import existing_index

OWNERS = (("granite__amazon_blue_A", "Amazon Blue"), ("granite__amazonia_B", "Amazonia"),
          ("granite__verde_ubatuba_C", "Verde Ubatuba"))


def _imports():
    """Three SAME-TYPE granites that all list 'Amazon Green Granite' as an alias; none is named that. Both the full
    spelling and the type-stripped one the cleaner produces ('Amazon Green')."""
    return existing_index(*((key, nm, "Amazon Green Granite|Amazon Green") for key, nm in OWNERS))


def _row(method: str, nearest: str | None) -> CanonicalRow:
    g = TreeGap(src_site="marenostone", surrogate_key="1", raw_name="Amazon Green Granite Slab",
                normalized_name="amazon green granite", gap_kind=GapKind.missing_variation,
                nearest_existing=nearest)
    return CanonicalRow(src_site="marenostone", surrogate_key="1", variety_match_key="Amazon Green Granite",
                        raw_type="Granite", variation_method=method, tree_gaps=[g])


def _minted(res) -> bool:
    return bool(res.new_variants)


def _card(res):
    return next((p for p in res.pending_confirm if p["variant"].lower().startswith("amazon green")), None)


def test_matcher_collision_holds_with_the_owners_it_named(monkeypatch):
    monkeypatch.setattr(curate, "load_existing", _imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()
    res = curate.build_curation([_row("exact_collision", "Amazon Blue, Amazonia, Verde Ubatuba")], ref)
    assert not res.learned_proposals, "must not alias onto the first same-type owner"
    assert not _minted(res), "must not mint a duplicate"
    card = _card(res)
    assert card and "Matches several existing varieties" in card["reason"]
    assert all(n in card["reason"] for n in ("Amazon Blue", "Amazonia", "Verde Ubatuba"))
    assert card["stone_type"] == "Granite" and card["src"] == "marenostone"


def test_a_typed_gap_with_several_same_type_owners_and_no_canonical_also_holds(monkeypatch):
    # the pre-existing curate path (no matcher verdict): the cleaned surface is owned by three same-type
    # varieties, none named that -> the same card, not sorted(owners)[0]
    monkeypatch.setattr(curate, "load_existing", _imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()
    res = curate.build_curation([_row("no_candidate", None)], ref)
    assert not res.learned_proposals and not _minted(res)
    card = _card(res)
    assert card and "Matches several existing varieties" in card["reason"] and "Amazonia" in card["reason"]


def test_a_single_same_type_owner_still_aliases(monkeypatch):
    # the unambiguous case must not regress into a hold: one same-type owner -> alias onto it
    # one owner only, whose full spelling is NEW -> an alias addition
    monkeypatch.setattr(curate, "load_existing", lambda: existing_index((OWNERS[0][0], OWNERS[0][1], "Amazon Green")))
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()
    res = curate.build_curation([_row("no_candidate", None)], ref)
    assert [a["identity_key"] for a in res.learned_proposals] == ["granite__amazon_blue_A"]
    assert _card(res) is None




def test_a_statement_binds_the_spelling_for_that_vendor_only(tmp_path, monkeypatch):
    from stone_pipeline.config import varieties
    from stone_pipeline.config.control import server
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: (n, t) == ("Golden Lightning", "Granite"))
    monkeypatch.setattr(varieties, "alias_target", lambda n, t: None)
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Amazon Green Granite",
                                  "name": "Golden Lightning", "type": "Granite"})
    assert code == 200 and body["result"] == "bound"
    assert views.scoped() == {("marenostone", "amazon green granite"): ("Golden Lightning", "Granite")}
    assert views.actions() == {}          # scoped: NOT a global decision


def _marble_imports():
    """Two SAME-TYPE marbles share the with-type alias 'Absolute Black Marble'; only ONE of them also carries
    the type-stripped form 'Absolute Black' the cleaner produces (on dev: Toros Black and Alaska
    White). So the cleaned surface has a single marble owner while the matcher's verdict names two."""
    return existing_index(("marble__toros_black_T", "Toros Black", "Absolute Black Marble"),
                          ("marble__alaska_white_A", "Alaska White", "Absolute Black Marble|Absolute Black"))


def test_a_matcher_collision_is_never_narrowed_away_by_the_cleaned_surface(monkeypatch):
    # The matcher held 'Absolute Black Marble Slab' as a collision of two marbles. Curate consulted the
    # matcher's owners only when the cleaned surface ('Absolute Black') had none; here it had one (Alaska
    # White), so curate 'aliased' onto an alias that already existed: no card, no mint, and the row held on
    # every produce with nothing to decide. The matcher's verdict must WIDEN the owner set, never lose to
    # a narrower surface: two same-type owners -> the collision card naming both.
    monkeypatch.setattr(curate, "load_existing", _marble_imports)
    monkeypatch.setattr(curate, "_alias_model", lambda: (None, {}))
    ref = loaders.load_all()
    g = TreeGap(src_site="marenostone", surrogate_key="S/08/18", raw_name="Absolute Black Marble Slab",
                normalized_name="absolute black marble", gap_kind=GapKind.missing_variation,
                nearest_existing="Alaska White, Toros Black")
    row = CanonicalRow(src_site="marenostone", surrogate_key="S/08/18", variety_match_key="Absolute Black Marble",
                       raw_type="Marble", variation_method="exact_collision", tree_gaps=[g])
    res = curate.build_curation([row], ref)
    assert not res.learned_proposals and not _minted(res)
    card = next((p for p in res.pending_confirm if p["variant"].lower().startswith("absolute black")), None)
    assert card and "Matches several existing varieties" in card["reason"], res.pending_confirm
    assert "Toros Black" in card["reason"] and "Alaska White" in card["reason"]
    assert card["stone_type"] == "Marble"
