"""After every produce, each stored decision is checked against the outcome; a decision the produce did not
honour is a `decision_gap` card. Two-pass is not a gap; a decision with no listing this produce is left alone."""

from __future__ import annotations

from stone_pipeline.config import server, store
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.stages import decision_audit, decisions


def _row(src, key, name="", stype="", origin="", osrc=""):
    return CanonicalRow(src_site=src, surrogate_key="1", variety_match_key=key, variation_name=name or None,
                        type_name=stype or None, origin_country_code=origin or None, origin_source=osrc or "")


EXISTING = {("golden lightning", "granite"), ("azul white", "onyx")}


def test_mint_is_a_gap_until_its_variety_exists():
    mints = {("", "bianco white marble"): {"action": "mint", "seed_name": "Bianco White", "seed_type": "Marble",
                                     "seed_country": "IR", "source": "marenostone", "variant_display": "Bianco White Marble"}}
    gaps = decision_audit.audit([], EXISTING, set(), mints, {}, {}, set())
    assert [g["decision"] for g in gaps] == ["mint"] and "Bianco White" in gaps[0]["reason"]
    assert decision_audit.audit([], EXISTING, {("bianco white", "marble")}, mints, {}, {}, set()) == []
    assert decision_audit.audit([], EXISTING | {("bianco white", "marble")}, set(), mints, {}, {}, set()) == []


def test_reject_is_a_gap_while_its_card_remains():
    mints = {("", "junk code"): {"action": "reject", "seed_name": None, "seed_type": None, "source": ""}}
    assert decision_audit.audit([], EXISTING, set(), mints, {}, {}, {"junk code"})[0]["decision"] == "reject"
    assert decision_audit.audit([], EXISTING, set(), mints, {}, {}, set()) == []


def test_alias_is_a_gap_when_the_listing_bound_elsewhere_but_not_for_a_new_target_or_no_listing():
    scoped = {("marenostone", "amazon green granite"): ("Golden Lightning", "Granite")}
    bound_right = [_row("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite")]
    assert decision_audit.audit(bound_right, EXISTING, set(), {}, scoped, {}, set()) == []
    bound_wrong = [_row("marenostone", "Amazon Green Granite", "Amazonia", "Granite")]
    gaps = decision_audit.audit(bound_wrong, EXISTING, set(), {}, scoped, {}, set())
    assert [g["decision"] for g in gaps] == ["alias"] and "Amazonia" in gaps[0]["reason"]
    unbound = [_row("marenostone", "Amazon Green Granite", "", "Granite")]
    assert decision_audit.audit(unbound, EXISTING, set(), {}, scoped, {}, set())[0]["decision"] == "alias"
    # the target was minted THIS produce: the listing binds next run, not a gap
    assert decision_audit.audit(unbound, EXISTING, {("golden lightning", "granite")}, {}, scoped, {}, set()) == []
    # no listing of that spelling this produce: nothing to judge
    assert decision_audit.audit([_row("zucchi", "Other", "X", "Granite")], EXISTING, set(), {}, scoped, {}, set()) == []


def test_origin_is_a_gap_when_a_bound_product_does_not_carry_it_at_the_operator_rung():
    origins = {("marenostone", "golden lightning", "granite"): "IR"}
    ok = [_row("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite", "IR", "supplier_override")]
    assert decision_audit.audit(ok, EXISTING, set(), {}, {}, origins, set()) == []
    off = [_row("marenostone", "Amazon Green Granite", "Golden Lightning", "Granite", "BR", "vendor_origin")]
    gaps = decision_audit.audit(off, EXISTING, set(), {}, {}, origins, set())
    assert [g["decision"] for g in gaps] == ["origin"] and "BR/vendor_origin" in gaps[0]["reason"]
    assert decision_audit.audit([], EXISTING, set(), {}, {}, origins, set()) == []      # no product bound yet


def test_gaps_surface_in_the_one_list_and_clear_when_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "config_db_path", lambda: tmp_path / "config.db")
    n = decisions.write_decision_gaps([{"kind": "decision_gap", "decision": "mint", "source": "marenostone",
                                        "scraped": "Bianco White Marble", "name": "Bianco White",
                                        "stone_type": "Marble", "origin": "IR", "reason": "Mint not applied"}])
    assert n == 1
    code, body = server.dispatch("GET", ["review", "variants"], None)
    card = next(c for c in body["variants"] if c["kind"] == "decision_gap")
    assert card["variant"] == "Bianco White" and card["src"] == "marenostone"
    assert card["listings"] == [{"source": "marenostone", "scraped": "Bianco White Marble"}]
    assert card["current_action"] == "mint" and card["reason"] == "Mint not applied"
    assert decisions.write_decision_gaps([]) == 0
    assert [c for c in server.dispatch("GET", ["review", "variants"], None)[1]["variants"] if c["kind"] == "decision_gap"] == []


def test_mint_gap_message_names_the_scraped_spelling_as_written():
    # the decision row exposes the display form as "spelling"; the gap must show 'Bianco White Marble', not
    # the normalised 'bianco white marble'
    mints = {("", "bianco white marble"): {"action": "mint", "seed_name": None, "seed_type": "Marble",
                                          "seed_country": "IR", "source": "", "spelling": "Bianco White Marble"}}
    gaps = decision_audit.audit([], EXISTING, set(), mints, {}, {}, set())
    assert gaps and gaps[0]["name"] == "Bianco White Marble"
