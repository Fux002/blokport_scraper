"""Decisions: the one object every stage reads. Three contracts:
  * DERIVATION: built from statement rows, a global 'is' is a mint, a vendor 'is' is that vendor's binding and
    a mint only while its variety does not exist (or is not a known alias); origins ride on the binding;
  * ORDER: one resolution rule (vendor then global, spelling then cleaned identity), per aspect, so a vendor
    statement without a colour still lets the global one answer the colour question;
  * a fresh store is empty and no file is created by a read."""

from __future__ import annotations

from stone_pipeline.config import decisions_store
from stone_pipeline.config.decisions_model import Decisions


def _no(*_a, **_k):
    return False


def _none(*_a, **_k):
    return None


def test_derivation_from_statements(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    exists = lambda n, t: (n, t) == ("Andes", "Quartzite")
    alias = lambda n, t: "Andes" if (n, t) == ("Artemis", "Quartzite") else None
    # a global mint with every seed, asked by zucchi; a vendor's own stone beside a spelling that means Andes;
    # a vendor binding through a known alias with a widened origin; a global reject
    decisions_store.decide("zucchi", "Honey Onyx", "Honey", "Onyx", color="Gold", origin="IR", widen=True,
                           exists_as=_no, alias_target=_none, clean=lambda s, t: s)
    decisions_store.decide("polonine", "Artemis", "Artemis", "Quartzite", origin="BR", widen=True,
                           exists_as=exists, alias_target=alias, clean=lambda s, t: s)
    decisions_store.decide("varsha", "Andes", "Andes Verde", "Quartzite",
                           exists_as=exists, alias_target=alias, clean=lambda s, t: s)
    decisions_store.reject("", "Junk Code")
    dec = decisions_store.load_decisions(exists_as=exists, alias_target=alias)
    assert {k: s.verdict for k, s in dec.statements.items()} == {
        ("", "honey onyx"): "is", ("varsha", "andes"): "is", ("", "junk code"): "reject"}
    honey = dec.statements[("", "honey onyx")]
    assert (honey.name, honey.stone_type, honey.color, honey.origin_iso, honey.asked_by) == ("Honey", "Onyx", "Gold", "IR", "zucchi")
    assert dec.bindings == {("polonine", "artemis"): ("Andes", "Quartzite"), ("varsha", "andes"): ("Andes Verde", "Quartzite")}
    assert dec.vendor_origins == {("polonine", "andes", "quartzite"): "BR", ("zucchi", "honey", "onyx"): "IR"}
    assert dec.widened == dec.vendor_origins
    assert dec.seed_types() == {("", "honey onyx"): "Onyx", ("varsha", "andes"): "Quartzite"}
    assert dec.mint_origin_rules() == {("honey", "onyx"): "IR"}
    # once the vendor's own stone exists, its statement is a binding only: nothing left to mint
    later = decisions_store.load_decisions(exists_as=lambda n, t: (n, t) in {("Andes", "Quartzite"), ("Andes Verde", "Quartzite")},
                                           alias_target=alias)
    assert ("varsha", "andes") not in later.statements and later.bindings[("varsha", "andes")] == ("Andes Verde", "Quartzite")


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
