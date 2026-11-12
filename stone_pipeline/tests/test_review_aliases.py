"""P2-10 C6 (PHASE-2-SPEC WP2.3 "Review API" and "Lifecycle", P2-10-SCRAPER-PLAN row C6): the review API over
config.db `learned_alias` (C2), and the lifecycle around it.

  * GET /config/v1/review/aliases[?status=]: every learned alias as ONE card shape, with the evidence the P2-11
    AliasesPanel card shows (the value, the variety it attaches to, the vendor, the method and score, the decision);
  * PUT /config/v1/review/aliases/{id} {action: approve|reject}: approve makes the row ACTIVE (a matcher input),
    reject makes it REJECTED (never proposed again for that variety), on a proposal or on an approved alias;
  * DELETE /config/v1/review/aliases/{id}: the ONLY way a learned value is removed; a deleted norm may be proposed
    again (forgotten, not rejected);
  * {id} is "<identity_key>|<norm>", url-encoded; match_key never yields '|';
  * unmint drops the learned rows of every Key of the variety, retire drops the Key's rows, un_retire does not bring
    them back (the matcher re-proposes); NO reset (soft, hard, pristine) touches learned_alias (the manager's Q5);
  * the review variant card carries the server's bind-or-mint resolution of its prefilled name and type, read from
    the same index decide() binds through (spec :178: "the existing card read returning the server's resolution").
Hermetic: tmp config.db and ledger, dispatch without sockets. No em dashes.
"""

from __future__ import annotations

from contextlib import closing
from urllib.parse import quote

import pytest

from stone_pipeline import lifecycle
from stone_pipeline.config import decisions_store, store, varieties
from stone_pipeline.config.control import server
from stone_pipeline.core.text import match_key
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.tests import test_reset_config_coordination as reset_tests

KEY = "granite__kashmir_gold_a"
SIBLING = "granite__kashmir_gold_b"
BYSTANDER = "marble__bianco_carrara_c"
CARD_FIELDS = {"id", "identity_key", "variety", "stone_type", "value", "norm", "method", "source", "score",
               "status", "first_seen", "decided_by", "decided_at"}


@pytest.fixture
def config_db(tmp_path, monkeypatch):
    path = tmp_path / "config.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(path))
    return path


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A ledger holding the varieties the learned rows name (the card's variety and type come from it)."""
    path = tmp_path / "development.db"
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(path))
    now = now_iso()
    with Ledger.open(path, env="development") as lg:
        for key, name, type_ in ((KEY, "Kashmir Gold", "Granite"), (SIBLING, "Kashmir Gold", "Granite"),
                                 (BYSTANDER, "Bianco Carrara", "Marble")):
            lg.upsert("variation", {"key": key, "type": type_, "name": name, "aliases": "[]", "medusa_id": None,
                                    "in_full": 1, "payload_hash": "", "state": "pending", "first_seen": now,
                                    "last_synced": None, "created_at": now, "updated_at": now}, pk=("key",))
    return path


def _propose(value, key=KEY, method="fuzzy", source="zucchi", score=None):
    decisions_store.propose_learned_aliases([{"identity_key": key, "value": value, "method": method,
                                              "source": source, "score": score}])


def _id(value, key=KEY) -> str:
    return f"{key}|{match_key(value)}"


def _path_id(value, key=KEY) -> str:
    return quote(_id(value, key), safe="")


def _rows() -> list[dict]:
    with closing(store.read_store()) as conn:
        return [dict(r) for r in conn.execute("SELECT identity_key, norm, status FROM learned_alias "
                                               "ORDER BY identity_key, norm")]


def _cards(query: str = "") -> dict:
    code, body = server.dispatch("GET", ["review", "aliases"], None, query)
    assert code == 200
    return body


def _decide(value, action, key=KEY):
    return server.dispatch("PUT", ["review", "aliases", _path_id(value, key)], {"action": action})


# --- GET: the cards and their evidence -------------------------------------------------------------------------------

def test_the_card_carries_every_evidence_field(config_db, ledger):
    _propose("Kashmir Golda", score=91.0)
    body = _cards()
    assert body["counts"] == {"proposed": 1, "active": 0, "rejected": 0}
    [card] = body["aliases"]
    assert set(card) == CARD_FIELDS
    assert card["id"] == _id("Kashmir Golda")
    assert (card["identity_key"], card["variety"], card["stone_type"]) == (KEY, "Kashmir Gold", "Granite")
    assert (card["value"], card["norm"], card["method"], card["source"]) == (
        "Kashmir Golda", "kashmir golda", "fuzzy", "zucchi")
    assert card["score"] == pytest.approx(91.0)
    assert card["status"] == "proposed" and card["first_seen"]
    assert card["decided_by"] is None and card["decided_at"] is None


def test_an_unknown_score_is_served_as_null(config_db, ledger):
    _propose("Kashmir Golda")
    assert _cards()["aliases"][0]["score"] is None


def test_cards_are_sorted_by_key_then_norm_and_filter_by_status(config_db, ledger):
    _propose("Bianko Carara", key=BYSTANDER)
    _propose("Kashmir Goldd")
    _propose("Kashmir Golda")
    _decide("Kashmir Goldd", "approve")
    assert [c["id"] for c in _cards()["aliases"]] == [
        _id("Kashmir Golda"), _id("Kashmir Goldd"), _id("Bianko Carara", BYSTANDER)]
    assert [c["value"] for c in _cards("status=active")["aliases"]] == ["Kashmir Goldd"]
    assert [c["value"] for c in _cards("status=proposed")["aliases"]] == ["Kashmir Golda", "Bianko Carara"]
    assert _cards("status=rejected")["aliases"] == []


def test_a_bad_status_filter_is_400(config_db, ledger):
    code, body = server.dispatch("GET", ["review", "aliases"], None, "status=maybe")
    assert code == 400 and "status" in body["error"]


def test_a_key_the_ledger_no_longer_holds_shows_no_variety(config_db, ledger):
    _propose("Ghost Spelling", key="granite__gone_z")
    [card] = _cards()["aliases"]
    assert card["identity_key"] == "granite__gone_z" and card["variety"] is None and card["stone_type"] is None


def test_no_ledger_is_a_503_naming_it(config_db, tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "absent.db"))
    _propose("Kashmir Golda")
    code, body = server.dispatch("GET", ["review", "aliases"], None, "")
    assert code == 503 and "ledger" in body["error"]


# --- PUT: approve and reject ----------------------------------------------------------------------------------------

def test_approve_makes_the_row_active_and_a_matcher_input(config_db, ledger):
    _propose("Kashmir Golda")
    code, body = _decide("Kashmir Golda", "approve")
    assert code == 200 and body == {"id": _id("Kashmir Golda"), "status": "active"}
    [card] = _cards()["aliases"]
    assert card["status"] == "active" and card["decided_by"] == "operator" and card["decided_at"]
    assert [(r["identity_key"], r["value"]) for r in decisions_store.active_learned_aliases()] == [
        (KEY, "Kashmir Golda")]


def test_reject_makes_a_proposal_rejected_and_it_is_never_proposed_again(config_db, ledger):
    _propose("Kashmir Golda")
    code, body = _decide("Kashmir Golda", "reject")
    assert code == 200 and body == {"id": _id("Kashmir Golda"), "status": "rejected"}
    _propose("KASHMIR-GOLDA", method="phonetic", source="other")     # the same norm, another spelling and vendor
    [card] = _cards()["aliases"]
    assert (card["status"], card["value"], card["source"]) == ("rejected", "Kashmir Golda", "zucchi")
    assert decisions_store.active_learned_aliases() == []


def test_reject_withdraws_an_approved_alias(config_db, ledger):
    _propose("Kashmir Golda")
    _decide("Kashmir Golda", "approve")
    assert _decide("Kashmir Golda", "reject")[0] == 200
    assert _cards()["aliases"][0]["status"] == "rejected"
    assert decisions_store.active_learned_aliases() == []


@pytest.mark.parametrize("body", [{"action": "maybe"}, {"action": ""}, {}, "approve", None])
def test_a_bad_action_or_body_is_400_and_changes_nothing(config_db, ledger, body):
    _propose("Kashmir Golda")
    code, out = server.dispatch("PUT", ["review", "aliases", _path_id("Kashmir Golda")], body)
    assert code == 400 and "action" in out["error"]
    assert _rows()[0]["status"] == "proposed"


def test_an_unknown_id_is_404(config_db, ledger):
    _propose("Kashmir Golda")
    code, body = _decide("Kashmir Goldy", "approve")
    assert code == 404 and _id("Kashmir Goldy") in body["error"]


@pytest.mark.parametrize("bad", ["no-separator", f"{KEY}|", f"|{match_key('Kashmir Golda')}", "%7C"])
def test_a_malformed_id_is_400(config_db, ledger, bad):
    _propose("Kashmir Golda")
    for method, body in (("PUT", {"action": "approve"}), ("DELETE", None)):
        code, out = server.dispatch(method, ["review", "aliases", quote(bad, safe="")], body)
        assert code == 400 and "id" in out["error"]
    assert _rows()[0]["status"] == "proposed"


# --- DELETE: the only removal -----------------------------------------------------------------------------------------

def test_a_delete_removes_the_value_and_it_may_be_proposed_again(config_db, ledger):
    _propose("Kashmir Golda")
    _propose("Kashmir Goldd")
    _decide("Kashmir Golda", "reject")
    code, body = server.dispatch("DELETE", ["review", "aliases", _path_id("Kashmir Golda")], None)
    assert code == 200 and body == {"id": _id("Kashmir Golda"), "deleted": True}
    assert [r["norm"] for r in _rows()] == ["kashmir goldd"]                      # only that row went
    _propose("Kashmir Golda")                                                     # forgotten, not rejected
    assert {r["norm"]: r["status"] for r in _rows()} == {"kashmir golda": "proposed", "kashmir goldd": "proposed"}


def test_a_delete_of_an_unknown_id_is_404(config_db, ledger):
    code, body = server.dispatch("DELETE", ["review", "aliases", _path_id("Kashmir Golda")], None)
    assert code == 404 and _id("Kashmir Golda") in body["error"]


def test_the_writes_are_durable_routes():
    """approve/reject and delete record an operator decision in config.db, so each is snapshotted on success
    (_durable), like every other review write."""
    routes = {(m, p): h for m, p, h in server._ROUTES}
    assert ("GET", ("review", "aliases")) in routes
    for method in ("PUT", "DELETE"):
        handler = routes[(method, ("review", "aliases", "{id}"))]
        assert getattr(handler, "snapshots_config", False), f"{method} review/aliases is not _durable"


def test_no_token_is_refused_before_any_route_runs(monkeypatch):
    calls: list = []
    monkeypatch.setattr(server, "dispatch", lambda *a, **k: calls.append(a) or (200, {}))
    for header in ("", "Bearer wrong"):
        h = server.ConfigHandler.__new__(server.ConfigHandler)
        h.headers = {"Authorization": header} if header else {}
        h.server = type("S", (), {"expected_token": "sekret"})()
        h.path = "/config/v1/review/aliases"
        answered: list = []
        h._respond = lambda code, payload: answered.append(code)
        h._handle("GET")
        assert answered == [401]
    assert calls == []


# --- the lifecycle ----------------------------------------------------------------------------------------------------

def _learned_on_every_key():
    for key in (KEY, SIBLING, BYSTANDER):
        _propose("Learned One", key=key)
        _propose("Learned Two", key=key)
    _decide("Learned One", "approve", key=KEY)
    _decide("Learned Two", "reject", key=SIBLING)


def test_an_unmint_drops_the_learned_rows_of_every_key_of_the_variety(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    captured: dict = {}
    reset_tests._stub_ledger_and_sync(monkeypatch, captured)   # Crystal White a/b/c + Absolute Black d
    keys = ["granite__crystal_white_a", "granite__crystal_white_b", "granite__crystal_white_c",
            "granite__absolute_black_d"]
    for key in keys:
        _propose("Learned One", key=key)
    _decide("Learned One", "approve", key=keys[0])
    result, code = lifecycle.unmint_variation(keys[1])
    assert code == 200 and result["learned_aliases_cleared"] == 3
    assert [r["identity_key"] for r in _rows()] == ["granite__absolute_black_d"]


def test_an_unmint_that_removes_nothing_keeps_every_learned_row(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    reset_tests._stub_ledger_and_sync(monkeypatch, {}, live=2)  # live products, no force: nothing is removed
    _propose("Learned One", key="granite__crystal_white_a")
    assert lifecycle.unmint_variation("granite__crystal_white_a")[1] == 409
    assert len(_rows()) == 1


def test_a_retire_drops_the_keys_rows_and_un_retire_does_not_restore_them(config_db, monkeypatch):
    from stone_pipeline.stages import decisions

    class _Sync:
        def variation_live_products(self, lg, key):
            return 0

        def retire_variation(self, lg, key, force=False):
            return {"retired": key}

        def un_retire_variation(self, lg, key):
            return {"un_retired": key}

    class _LG:
        def get(self, table, col, key):
            return {"key": key}

    monkeypatch.setattr(lifecycle, "_ledger_op", lambda name, work: (work(_LG(), _Sync()), 200))
    monkeypatch.setattr(decisions, "add_retired", lambda key: None)
    monkeypatch.setattr(decisions, "remove_retired", lambda key: None)
    _propose("Learned One", key=KEY)
    _propose("Learned One", key=SIBLING)
    _decide("Learned One", "approve", key=KEY)
    result, code = lifecycle.retire_variation(KEY)
    assert code == 200 and result["learned_aliases_cleared"] == 1
    assert [r["identity_key"] for r in _rows()] == [SIBLING]                    # retire is Key-scoped
    assert lifecycle.un_retire(KEY)[1] == 200
    assert [r["identity_key"] for r in _rows()] == [SIBLING]                    # the matcher re-proposes, not un_retire


@pytest.mark.parametrize("mode", [{"hard": False}, {"hard": True}, {"pristine": True}])
def test_no_reset_touches_a_learned_row(tmp_path, monkeypatch, mode):
    from stone_pipeline.io import image_wipe
    from stone_pipeline.ledger import snapshot
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n", encoding="utf-8")
    reset_tests._seed_config(tmp_path, monkeypatch)
    store.seed_from_yaml(yaml_path=yaml_path)
    _learned_on_every_key()
    before = _rows()
    monkeypatch.setattr(lifecycle, "_ledger_op", reset_tests._fake_ledger_op)
    monkeypatch.setattr(image_wipe, "wipe_all_product_images", lambda **k: {})
    monkeypatch.setattr(snapshot, "wipe_artifacts", lambda **k: {"local": [], "s3": []})
    for sources in (None, ["polonine"]):                          # the global and the scoped reset
        out, code = lifecycle.reset(sources=sources, **mode)
        assert code == 200
        assert "learned_alias" not in (out.get("config") or {})
        assert _rows() == before


# --- "Will mint": the server's resolution on the review card ---------------------------------------------------------

def _variant_card(**payload):
    decisions_store.replace_pending("variety", [{"ref": match_key(payload.get("variant", "x")), "payload": payload}])
    code, body = server.dispatch("GET", ["review", "variants"], None, "")
    assert code == 200
    return body["variants"][0]


@pytest.fixture
def index(monkeypatch):
    """The index decide() binds through: 'Andes' is a Quartzite, 'Artemis' its alias; 'Andes' is no Marble."""
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: (match_key(n), match_key(t)) == ("andes", "quartzite"))
    monkeypatch.setattr(varieties, "alias_target",
                        lambda n, t: "Andes" if (match_key(n), match_key(t)) == ("artemis", "quartzite") else None)


def test_a_known_alias_reads_bind_to_its_variety_not_will_mint(config_db, index):
    card = _variant_card(variant="Artemis", stone_type="Quartzite", src="zucchi", scraped="Artemis Quartzite")
    assert card["resolution"] == {"action": "bind", "name": "Andes", "stone_type": "Quartzite"}


def test_an_existing_variety_reads_bind(config_db, index):
    card = _variant_card(variant="andes", stone_type="Quartzite", src="zucchi", scraped="Andes")
    assert card["resolution"] == {"action": "bind", "name": "andes", "stone_type": "Quartzite"}


def test_a_new_name_or_another_type_reads_mint(config_db, index):
    card = _variant_card(variant="Andes", stone_type="Marble", src="zucchi", scraped="Andes Marble")
    assert card["resolution"] == {"action": "mint", "name": "Andes", "stone_type": "Marble"}


def test_the_decided_name_and_type_are_resolved_before_the_scraped_guess(config_db, index):
    from stone_pipeline.tests import _decision_views as views
    views.mint("Artemis Q", stone_type="Quartzite", name="Artemis", source="zucchi")
    card = _variant_card(variant="Artemis Q", stone_type="Marble", src="zucchi", scraped="Artemis Q",
                         listings=[{"source": "zucchi", "scraped": "Artemis Q"}])
    assert card["resolution"] == {"action": "bind", "name": "Andes", "stone_type": "Quartzite"}


def test_a_card_with_no_name_or_type_has_no_resolution(config_db, index):
    assert _variant_card(variant="Andes", stone_type="", src="zucchi", scraped="Andes")["resolution"] is None


def test_decide_binds_exactly_where_the_card_reads_bind(config_db, index):
    """One rule: the card's resolution and decide()'s outcome come from the same function."""
    card = _variant_card(variant="Artemis", stone_type="Quartzite", src="zucchi", scraped="Artemis Quartzite")
    outcome = decisions_store.decide("zucchi", "Artemis Quartzite", "Artemis", "Quartzite",
                                     clean=lambda s, t: s)
    assert outcome["result"] == "bound" and outcome["name"] == card["resolution"]["name"]


# --- the cleanup's open review items (read-only; the file format is C5's aliases.open_items) -------------------------

@pytest.fixture
def review_dir(tmp_path, monkeypatch):
    import dataclasses
    from stone_pipeline.aliases import review
    from stone_pipeline.config.settings import SETTINGS
    path = tmp_path / "review"
    path.mkdir()
    monkeypatch.setattr(review, "SETTINGS",
                        dataclasses.replace(SETTINGS, paths=dataclasses.replace(SETTINGS.paths, review_dir=path)))
    return path


def test_the_open_items_are_served_read_only_beside_the_cards(config_db, ledger, review_dir):
    from stone_pipeline.aliases.open_items import DIGIT_REASON, OPEN_FILE, open_items_bytes
    (review_dir / OPEN_FILE).write_bytes(open_items_bytes([
        ("digit_non_code", KEY, "Kashmir Gold 2", DIGIT_REASON),
        ("admin_refused", BYSTANDER, "G603", "is_code")]))
    body = _cards()
    assert body["open_read"] == "current"
    assert body["open"] == [
        {"kind": "admin_refused", "key": BYSTANDER, "value": "G603", "reason": "is_code"},
        {"kind": "digit_non_code", "key": KEY, "value": "Kashmir Gold 2", "reason": DIGIT_REASON}]


def test_no_open_file_reads_as_absent_never_as_nothing_open(config_db, ledger, review_dir):
    body = _cards()
    assert body["open"] == [] and body["open_read"] == "absent"


def test_an_unreadable_open_file_is_a_503_naming_it(config_db, ledger, review_dir):
    from stone_pipeline.aliases.open_items import OPEN_FILE
    (review_dir / OPEN_FILE).write_text("what,ever\n", encoding="utf-8")
    code, body = server.dispatch("GET", ["review", "aliases"], None, "")
    assert code == 503 and OPEN_FILE in body["error"]


def test_an_open_item_has_no_decision_route(config_db, ledger, review_dir):
    """Admin rows stay staff-managed in BlokPort, and a digit item is the cleanup's to settle: no approve or reject."""
    code, _ = server.dispatch("PUT", ["review", "aliases", quote(f"{KEY}|kashmir gold 2", safe="")],
                              {"action": "approve"})
    assert code == 404
