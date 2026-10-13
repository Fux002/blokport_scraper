"""The operator's answer to "which slabs of this lot are sold?": validated against the card it answers,
stored durably in config.db for the stock figure it was given for, read back by the loader, and shown on the
card. Served to Blokport's admin at GET /config/v1/review/lots and PUT /config/v1/review/lots/<ref>."""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store, server

CARD = {"ref": "sentas|2351", "sources": ["sentas"], "payload": {
    "source": "sentas", "lot_ref": "2351", "label": "B4804", "variety": "Golden Emparador", "src_url": None,
    "available": 2, "sold_known": 2, "scanned": 4, "must_mark_sold": 2, "previous_answer": None,
    "pieces": [{"position": n, "image": f"scan-{n}"} for n in (1, 2, 3, 4)]}}


@pytest.fixture
def store_with_card(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    decisions_store.replace_pending("lot", [CARD])


def test_an_answer_is_stored_for_the_stock_figure_and_read_back_by_source(store_with_card):
    decisions_store.answer_lot("sentas|2351", [1, 3])
    assert decisions_store.lot_answers("sentas") == {"2351": (2, frozenset({1, 3}))}
    assert decisions_store.lot_answers("other") == {}


def test_the_card_shows_the_answer_until_the_next_produce_drops_it(store_with_card):
    assert decisions_store.list_pending("lot")[0]["decided"] is False
    decisions_store.answer_lot("sentas|2351", [3, 1])
    card = decisions_store.list_pending("lot")[0]
    assert card["decided"] is True and card["current_sold"] == [1, 3] and card["ref"] == "sentas|2351"


@pytest.mark.parametrize("sold, message", [
    ([1], "2 slabs in stock"),            # 3 unmarked would be offered, only 2 exist
    ([1, 9], "not a scanned slab"),       # 9 is not in the sequence
    ([1, 1, 3], "twice"),
    (["1", 3], "whole numbers"),
])
def test_an_answer_that_contradicts_the_card_is_refused(store_with_card, sold, message):
    with pytest.raises(decisions_store.InvalidDecision, match=message):
        decisions_store.answer_lot("sentas|2351", sold)
    assert decisions_store.lot_answers("sentas") == {}


def test_answering_a_lot_without_a_card_is_refused(store_with_card):
    with pytest.raises(LookupError):
        decisions_store.answer_lot("sentas|9999", [1])


# -- the admin API ---------------------------------------------------------------------------------------

def test_the_review_api_lists_and_answers_lot_cards(store_with_card):
    code, body = server.dispatch("GET", ["review", "lots"], None)
    assert code == 200 and [c["ref"] for c in body["lots"]] == ["sentas|2351"]
    code, body = server.dispatch("PUT", ["review", "lots", "sentas%7C2351"], {"sold": [2, 4]})
    assert code == 200 and body == {"ref": "sentas|2351", "sold": [2, 4]}
    assert server.dispatch("PUT", ["review", "lots", "sentas%7C2351"], {"sold": [2]})[0] == 400
    assert server.dispatch("PUT", ["review", "lots", "sentas%7C9999"], {"sold": [2, 4]})[0] == 404
    assert server.dispatch("PUT", ["review", "lots", "sentas%7C2351"], "not-an-object")[0] == 400


# -- the run writes its cards; the catalog puts the latest run's cards in the queue ----------------------

def test_the_latest_complete_run_s_cards_become_the_review_queue(tmp_path, monkeypatch):
    from stone_pipeline import catalog
    from stone_pipeline.stages import emit
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    for run, cards in (("sentas_20261001_000000", [CARD, {**CARD, "ref": "sentas|1"}]),
                       ("sentas_20261013_000000", [CARD])):
        (tmp_path / "out" / run / "diagnostics").mkdir(parents=True)
        (tmp_path / "out" / run / "diagnostics" / "canonical.parquet").write_bytes(b"x")
        (tmp_path / "out" / run / "review").mkdir()
        emit.write_lot_cards(cards, tmp_path / "out" / run / "review" / "lot_cards.json")
    assert catalog.collect_lot_cards(tmp_path / "out") == 1
    assert [c["ref"] for c in decisions_store.list_pending("lot")] == ["sentas|2351"]
