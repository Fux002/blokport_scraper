"""Review progress: a decided card stays in the pending list, is flagged, and sinks to the end.

It cannot leave the list on decision -- only the next produce binds it -- so the list length never moves
while the operator works. Without the flag, the order and the count there is no way to tell a settled card
from an untouched one, nor a finished review from an untouched one."""

from stone_pipeline.config import server


def _cards(monkeypatch, variety, origin=()):
    from stone_pipeline.config import decisions_store

    def fake(kind):
        return list(variety) if kind == "variety" else list(origin)
    monkeypatch.setattr(decisions_store, "list_pending", fake)
    status, body = server.dispatch("GET", ["review", "variants"], None)
    assert status == 200
    return body


def test_decided_cards_sink_to_the_end_and_are_flagged(monkeypatch):
    got = _cards(monkeypatch, [
        {"ref": "a", "variant": "A", "decided": False, "current_action": None},
        {"ref": "b", "variant": "B", "decided": True, "current_action": "alias"},
        {"ref": "c", "variant": "C", "decided": False, "current_action": None},
        {"ref": "d", "variant": "D", "decided": True, "current_action": "mint"},
    ])
    assert [c["ref"] for c in got["variants"]] == ["a", "c", "b", "d"]   # undecided first, order kept
    assert [c["decided"] for c in got["variants"]] == [False, False, True, True]


def test_counts_give_the_progress_signal(monkeypatch):
    got = _cards(monkeypatch, [
        {"ref": "a", "decided": True, "current_action": "mint"},
        {"ref": "b", "decided": False, "current_action": None},
        {"ref": "c", "decided": False, "current_action": None},
    ])
    assert got["counts"] == {"total": 3, "decided": 1, "undecided": 2}


def test_a_decided_card_is_never_dropped_from_the_list(monkeypatch):
    """The whole point: deciding records a statement, it does not bind. The card must survive."""
    got = _cards(monkeypatch, [{"ref": "a", "variant": "A", "decided": True, "current_action": "reject"}])
    assert [c["ref"] for c in got["variants"]] == ["a"]
    assert got["counts"] == {"total": 1, "decided": 1, "undecided": 0}


def test_origin_cards_carry_the_flag_too(monkeypatch):
    got = _cards(monkeypatch,
                 [{"ref": "v", "variant": "V", "decided": False, "current_action": None}],
                 [{"ref": "s|o|t", "variety": "O", "scraped": "o", "source": "s", "decided": True,
                   "current_country": "IT"}])
    assert [c["ref"] for c in got["variants"]] == ["v", "s|o|t"]     # decided origin card sinks
    assert got["variants"][-1]["kind"] == "origin" and got["variants"][-1]["decided"] is True
    assert got["counts"] == {"total": 2, "decided": 1, "undecided": 1}


def test_a_vendor_scoped_alias_counts_as_decided(monkeypatch, tmp_path):
    """The regression from prod: "this is an existing variety" is stored in scoped_alias, NOT
    variety_decision. Reading only variety_decision left every aliased card looking untouched."""
    from stone_pipeline.config import decisions_store as ds

    monkeypatch.setattr(ds, "variety_actions", lambda: {})            # no mint/reject anywhere
    monkeypatch.setattr(ds, "scoped_aliases",
                        lambda: {("zucchi", "agata dark blue"): ("Agata Blue", "Agate")})

    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    rows = [_Row(ref="agata dark blue", payload='{"variant": "Agata Dark Blue"}', sources=None),
            _Row(ref="untouched name", payload='{"variant": "Untouched"}', sources=None)]

    class _Cur:
        def fetchall(self): return rows

    class _Conn:
        def execute(self, *a, **k): return _Cur()
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())

    out = {i["ref"]: i for i in ds.list_pending("variety")}
    a = out["agata dark blue"]
    assert a["decided"] is True                 # the bug: was False
    assert a["current_action"] == "alias"       # and the UI showed nothing
    assert a["current_alias_of"] == "Agata Blue"
    assert a["current_seed_type"] == "Agate"
    assert out["untouched name"]["decided"] is False
