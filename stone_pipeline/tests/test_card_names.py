"""Review cards show names by their field's rule (plan W8): evidence for the operator, cleaned where it can be,
as scraped where it cannot, and the candidate list kept as it is (it names each candidate's type)."""

from __future__ import annotations

from stone_pipeline.stages import decisions


def _write(monkeypatch, row):
    seen = {}
    monkeypatch.setattr(decisions.decisions_store, "replace_pending", lambda kind, rows: seen.setdefault(kind, rows))
    decisions.write_confirm_file([row])
    return seen["variety"][0]


def test_a_card_shows_the_variety_and_colour_clean(monkeypatch):
    card = _write(monkeypatch, {"variant": "São Gabriel", "stone_type": "Granite", "color": "rosé",
                                "nearest_existing": "Copacabana (Granite); Panda Black (Granite)"})
    assert card["payload"]["variant"] == "Sao Gabriel"
    assert card["payload"]["color"] == "Rose"
    assert card["payload"]["nearest_existing"] == "Copacabana (Granite); Panda Black (Granite)"


def test_a_mixed_script_card_keeps_its_spelling_and_ref(monkeypatch):
    card = _write(monkeypatch, {"variant": "Nero 黑 Marquina", "stone_type": "Marble"})
    assert card["payload"]["variant"] == "Nero 黑 Marquina"
    assert card["ref"] == "nero marquina|marble"
