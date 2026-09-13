"""Decisions: the one object every stage reads. Two contracts:
  * PARITY: built from a real store by load_decisions(), its views equal the legacy accessors they replace;
  * ORDER: one resolution rule (vendor then global, spelling then cleaned identity), per aspect, so a vendor
    statement without a colour still lets the global one answer the colour question."""

from __future__ import annotations

from stone_pipeline.config import decisions_store
from stone_pipeline.config.decisions_model import Decisions


def _no(*_a, **_k):
    return False


def _none(*_a, **_k):
    return None


def _populate() -> None:
    # a global mint with every seed, a vendor-level mint (the spelling already means Andes for everyone), a
    # global reject, a vendor binding and a widened vendor origin
    decisions_store.decide("zucchi", "Honey Onyx", "Honey", "Onyx", color="Gold", origin="IR", widen=True,
                           exists_as=_no, alias_target=_none, clean=lambda s, t: s)
    decisions_store.decide("polonine", "Artemis", "Andes", "Quartzite",
                           exists_as=lambda n, t: n == "Andes", alias_target=_none, clean=lambda s, t: s)
    decisions_store.decide("varsha", "Brown Granite", "Brown Stone", "Granite",
                           exists_as=_no, alias_target=lambda n, t: "Brown Granite" if n == "Brown Granite" else None,
                           clean=lambda s, t: s)
    decisions_store.set_variety_decision("Junk Code", "reject")


def test_views_equal_the_legacy_accessors(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    _populate()
    dec = decisions_store.load_decisions()
    assert dec.seed_types() == decisions_store.variety_seed_types()
    assert dec.mint_origin_rules() == decisions_store.variety_seed_country_rules()
    assert dec.bindings == decisions_store.scoped_aliases()
    assert dec.vendor_origins == decisions_store.origin_decisions()
    assert dec.widened == decisions_store.origin_widen()
    confirm = decisions_store.confirm_map()
    assert {k for k, s in dec.statements.items() if s.is_mint} == {k for k, v in confirm.items() if v == "yes"}
    assert {k for k, s in dec.statements.items() if not s.is_mint} == decisions_store.rejected_names()
    assert {k: s.color for k, s in dec.statements.items() if s.is_mint and s.color} == decisions_store.variety_seed_colors()
    assert {k: s.name for k, s in dec.statements.items() if s.is_mint and s.name} == decisions_store.variety_seed_names()
    assert {k: s.source for k, s in dec.statements.items() if s.is_mint and s.source} == decisions_store.variety_seed_scopes()


def test_fresh_store_is_empty_and_creates_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))
    dec = decisions_store.load_decisions()
    assert dec == Decisions.empty()
    assert not (tmp_path / "absent.db").exists()


def test_resolution_order_vendor_then_global_spelling_then_clean():
    dec = Decisions.from_legacy({
        ("", "honey onyx"): {"action": "mint", "seed_type": "Onyx", "seed_color": "Gold", "seed_name": "Honey"},
        ("zucchi", "honey onyx"): {"action": "mint", "seed_type": "Onyx", "source": "zucchi"},   # own stone, no colour
        ("", "brown granite"): {"action": "reject"},
    })
    # the vendor's own statement wins where it answers; the global one answers what it does not carry
    assert dec.confirm("zucchi", "Honey Onyx", "Honey") == "yes"
    assert dec.scope("zucchi", "Honey Onyx", "Honey") == "zucchi"
    assert dec.scope("polonine", "Honey Onyx", "Honey") == ""
    assert dec.seed_color("zucchi", "Honey Onyx", "Honey") == "Gold"            # falls through to the global one
    assert dec.seed_name("polonine", "Honey Onyx", "Honey") == "Honey"
    # the cleaned identity is the second key at each level
    assert dec.seed_type("polonine", "Honey Onyx Slab", "Honey Onyx") == "Onyx"
    # a reject answers 'no' and is the reject memory; an unknown spelling is undecided
    assert dec.confirm("varsha", "Brown Granite", "Brown") == "no"
    assert dec.is_rejected("varsha", "Brown Granite", "Brown")
    assert dec.confirm("varsha", "Nothing", "Nothing") is None
    assert not dec.is_rejected("varsha", "Nothing", "Nothing")


def test_mint_origin_rules_key_the_created_name():
    dec = Decisions.from_legacy({
        ("", "honey onyx"): {"action": "mint", "seed_type": "Onyx", "seed_name": "Honey", "seed_country": "IR"},
        ("", "verde"): {"action": "mint", "seed_type": "Granite", "seed_country": "BR"},
    })
    assert dec.mint_origin_rules() == {("honey", "onyx"): "IR", ("verde", "granite"): "BR"}
