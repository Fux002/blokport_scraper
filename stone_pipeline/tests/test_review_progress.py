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

    rows = [_Row(ref="agata dark blue",
                 payload=('{"variant": "Agata Dark Blue", "src": "zucchi", "scraped": "Agata Dark Blue",'
                          ' "listings": [{"source": "zucchi", "scraped": "Agata Dark Blue"}]}'),
                 sources=None),
            _Row(ref="untouched name",
                 payload='{"variant": "Untouched", "src": "zucchi", "scraped": "Untouched"}', sources=None)]

    class _Cur:
        def fetchall(self): return rows

    class _Conn:
        def execute(self, *a, **k): return _Cur()
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())
    monkeypatch.setattr(ds, "origin_widen", lambda: {})

    out = {i["ref"]: i for i in ds.list_pending("variety")}
    a = out["agata dark blue"]
    assert a["decided"] is True                 # the bug: was False
    assert a["current_action"] == "alias"       # and the UI showed nothing
    assert a["current_alias_of"] == "Agata Blue"
    assert a["current_seed_type"] == "Agate"
    assert out["untouched name"]["decided"] is False


def test_decided_matches_scraped_spelling_not_the_cleaned_ref(monkeypatch):
    """The Gold-card bug: a card's ref is the CLEANED name (type word stripped), but decide() keys a mint
    and an alias on the SCRAPED spelling. Matching on ref alone missed every card whose cleaner stripped a
    suffix. Match on the card's spellings/listings instead."""
    from stone_pipeline.config import decisions_store as ds

    # alias stored under the SCRAPED spelling; card ref is the cleaned name
    monkeypatch.setattr(ds, "variety_actions", lambda: {})
    monkeypatch.setattr(ds, "scoped_aliases",
                        lambda: {("marenostone", "amazon green granite"): ("Golden Lightning", "Granite")})

    class _Cur:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows

    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    payload = ('{"variant": "Amazon Green", "src": "marenostone", "scraped": "Amazon Green Granite",'
               ' "spellings": ["Amazon Green Granite"],'
               ' "listings": [{"source": "marenostone", "scraped": "Amazon Green Granite"}]}')
    rows = [_Row(ref="amazon green", payload=payload, sources=None)]

    class _Conn:
        def execute(self, *a, **k): return _Cur(rows)
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())
    monkeypatch.setattr(ds, "origin_widen", lambda: {})

    card = ds.list_pending("variety")[0]
    assert card["ref"] == "amazon green"                 # cleaned name != scraped spelling
    assert card["decided"] is True                       # the bug: was False
    assert card["current_action"] == "alias"
    assert card["current_alias_of"] == "Golden Lightning"
    assert card["current_seed_type"] == "Granite"


def test_a_mint_keyed_on_the_scraped_spelling_is_found(monkeypatch):
    """decide() stores a mint under the scraped spelling, not the ref -- same mismatch class as the alias."""
    from stone_pipeline.config import decisions_store as ds
    monkeypatch.setattr(ds, "variety_actions",
                        lambda: {"blue dunes quartzite": {"action": "mint", "alias_of": None,
                                                          "seed_color": None, "seed_type": "Quartzite",
                                                          "seed_country": "BR", "seed_name": None}})
    monkeypatch.setattr(ds, "scoped_aliases", lambda: {})

    class _Cur:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows

    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    payload = ('{"variant": "Blue Dunes", "src": "zucchi", "scraped": "Blue Dunes Quartzite",'
               ' "spellings": ["Blue Dunes Quartzite"]}')
    rows = [_Row(ref="blue dunes", payload=payload, sources=None)]

    class _Conn:
        def execute(self, *a, **k): return _Cur(rows)
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())
    monkeypatch.setattr(ds, "origin_widen", lambda: {})

    card = ds.list_pending("variety")[0]
    assert card["decided"] is True and card["current_action"] == "mint"
    assert card["current_seed_type"] == "Quartzite"


def test_renamed_global_mint_keyed_on_a_listing_spelling_is_found(monkeypatch):
    """The reattachment gap Blokport flagged: a renamed GLOBAL mint is keyed on the scraped spelling
    (a listing value), with NO scoped_alias to catch it. If the card's `spellings` doesn't include that
    listing spelling, the flag missed -> a successful mint showed undecided on refresh."""
    from stone_pipeline.config import decisions_store as ds
    # mint keyed on the LISTING spelling, which is NOT in the card's display `spellings`
    monkeypatch.setattr(ds, "variety_actions",
                        lambda: {"brown granite slab 2cm": {"action": "mint", "alias_of": None,
                                 "seed_color": None, "seed_type": "Granite", "seed_country": "IR",
                                 "seed_name": "Chocolate Brown"}})
    monkeypatch.setattr(ds, "scoped_aliases", lambda: {})   # global mint: no scoped alias

    class _Cur:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    payload = ('{"variant": "Brown", "spellings": ["Brown Granite"],'          # display spelling differs
               ' "listings": [{"source": "marenostone", "scraped": "Brown Granite Slab 2cm"}]}')  # real key
    rows = [_Row(ref="brown", payload=payload, sources=None)]
    class _Conn:
        def execute(self, *a, **k): return _Cur(rows)
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())
    monkeypatch.setattr(ds, "origin_widen", lambda: {})

    card = ds.list_pending("variety")[0]
    assert card["decided"] is True                    # found via the listing spelling
    assert card["current_action"] == "mint"
    assert card["current_seed_type"] == "Granite"


def test_origin_card_decided_when_its_listing_is_bound(monkeypatch):
    """An origin card is keyed (source, variety, type), but confirming it BINDS the vendor spelling -- a
    scoped_alias on the listing (source, scraped). Reading only the origin country by ref missed the bind,
    so a confirmed origin card came back undecided (the Amazon White case)."""
    from stone_pipeline.config import decisions_store as ds

    monkeypatch.setattr(ds, "origin_decisions", lambda: {})              # no origin-country row for this ref
    monkeypatch.setattr(ds, "scoped_aliases",
                        lambda: {("marenostone", "amazon marble"): ("Silver Stream", "Marble")})  # listing bound

    class _Cur:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    payload = ('{"source": "marenostone", "variety": "Amazon White", "stone_type": "Marble",'
               ' "scraped": "Amazon Marble"}')
    rows = [_Row(ref="marenostone|amazon white|marble", payload=payload, sources=None)]
    class _Conn:
        def execute(self, *a, **k): return _Cur(rows)
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())
    monkeypatch.setattr(ds, "origin_widen", lambda: {})

    card = ds.list_pending("origin")[0]
    assert card["decided"] is True                    # the bug: was False
    assert card["current_action"] == "alias"
    assert card["current_alias_of"] == "Silver Stream"


def test_every_decided_card_names_its_action(monkeypatch):
    """Invariant Blokport relies on: decided:true always carries current_action (never a bare "Decided").
    Covers the origin-country case (action "origin") that previously had decided:true with no action."""
    from stone_pipeline.config import decisions_store as ds
    monkeypatch.setattr(ds, "variety_actions", lambda: {})
    monkeypatch.setattr(ds, "scoped_aliases", lambda: {})
    monkeypatch.setattr(ds, "origin_decisions",
                        lambda: {("marenostone", "amazon white", "marble"): "IR"})   # country only, no bind

    class _Cur:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    payload = ('{"source": "marenostone", "variety": "Amazon White", "stone_type": "Marble",'
               ' "scraped": "Amazon Marble"}')
    rows = [_Row(ref="marenostone|amazon white|marble", payload=payload, sources=None)]
    class _Conn:
        def execute(self, *a, **k): return _Cur(rows)
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())
    monkeypatch.setattr(ds, "origin_widen", lambda: {})

    card = ds.list_pending("origin")[0]
    assert card["decided"] is True
    assert card["current_action"] == "origin"          # was None before -> bare "Decided"


def test_widen_documented_origin_is_surfaced_on_the_card(monkeypatch):
    """When the operator ticked "add to documented origins" (widen), the card shows it -- widened=True and
    the documented country -- keyed on the DECIDED target variety, like the rest of the decision."""
    from stone_pipeline.config import decisions_store as ds
    monkeypatch.setattr(ds, "variety_actions", lambda: {})
    monkeypatch.setattr(ds, "scoped_aliases",
                        lambda: {("zucchi", "amazon marble"): ("Silver Stream", "Marble")})
    monkeypatch.setattr(ds, "origin_decisions", lambda: {})
    monkeypatch.setattr(ds, "origin_widen", lambda: {("zucchi", "silver stream", "marble"): "IR"})

    class _Cur:
        def __init__(self, rows): self._rows = rows
        def fetchall(self): return self._rows
    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    payload = ('{"variant": "Amazon Marble", "src": "zucchi", "scraped": "Amazon Marble",'
               ' "listings": [{"source": "zucchi", "scraped": "Amazon Marble"}]}')
    rows = [_Row(ref="amazon marble", payload=payload, sources=None)]
    class _Conn:
        def execute(self, *a, **k): return _Cur(rows)
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())

    card = ds.list_pending("variety")[0]
    assert card["current_action"] == "alias" and card["current_alias_of"] == "Silver Stream"
    assert card["widen"] is True and card["documented_origin"] == "IR"

    monkeypatch.setattr(ds, "origin_widen", lambda: {})
    card = ds.list_pending("variety")[0]
    assert card["widen"] is False and card["documented_origin"] is None


def _one_card(monkeypatch, actions, aliases, payload, ref="foo"):
    """Drive list_pending('variety') for a single hand-built card with the given decision stores."""
    from stone_pipeline.config import decisions_store as ds
    monkeypatch.setattr(ds, "variety_actions", lambda: actions)
    monkeypatch.setattr(ds, "scoped_aliases", lambda: aliases)
    monkeypatch.setattr(ds, "origin_widen", lambda: {})

    class _Row(dict):
        def __getitem__(self, k): return dict.__getitem__(self, k)

    class _Cur:
        def fetchall(self): return [_Row(ref=ref, payload=payload, sources=None)]

    class _Conn:
        def execute(self, *a, **k): return _Cur()
        def close(self): pass
    monkeypatch.setattr(ds.store, "open_store", lambda: _Conn())
    return ds.list_pending("variety")[0]


_FOO = ('{"variant": "Foo", "src": "vendor", "scraped": "Foo",'
        ' "listings": [{"source": "vendor", "scraped": "Foo"}]}')


def test_a_scoped_bind_supersedes_a_stale_mint_in_the_display(monkeypatch):
    """The card must show the LAST decision. A mint left behind under a different source (a global mint, a
    cross-vendor spelling) that clear_decisions cannot drop must NOT shadow the newer per-vendor bind: the
    card reads the alias, not the superseded mint."""
    card = _one_card(monkeypatch,
                     actions={"foo": {"action": "mint", "alias_of": None, "seed_color": "Grey",
                                      "seed_type": "Granite", "seed_country": "BR", "seed_name": "Old Mint"}},
                     aliases={("vendor", "foo"): ("New Target", "Granite")},
                     payload=_FOO)
    assert card["decided"] is True
    assert card["current_action"] == "alias"
    assert card["current_alias_of"] == "New Target"
    assert card["current_seed_type"] == "Granite"
    assert card["current_seed_name"] is None            # the stale mint's seeds are gone from the display


def test_a_mint_plus_rename_still_reads_as_the_mint(monkeypatch):
    """A mint+rename writes both a mint and a scoped alias pointing at the minted name -- that is ONE
    decision and must still read as the mint (not be mistaken for a superseding bind)."""
    card = _one_card(monkeypatch,
                     actions={"foo": {"action": "mint", "alias_of": None, "seed_color": "Grey",
                                      "seed_type": "Granite", "seed_country": "BR", "seed_name": "Corrected"}},
                     aliases={("vendor", "foo"): ("Corrected", "Granite")},
                     payload=_FOO)
    assert card["decided"] is True
    assert card["current_action"] == "mint"
    assert card["current_seed_name"] == "Corrected"


_TYPELESS = ('{"variant": "Foo", "src": "vendor", "scraped": "Foo", "stone_type": "",'
             ' "listings": [{"source": "vendor", "scraped": "Foo"}]}')


def test_a_decided_type_less_card_is_re_keyable_by_the_decided_type(monkeypatch):
    """A type-less scraped variety carries stone_type='' (the vendor declared no type). Once decided, the
    card must still expose a type so the operator can restate it -- backfilled from the decided (alias
    target / mint) type. Without it the UI has nothing to key on and Confirm cannot fire."""
    card = _one_card(monkeypatch,
                     actions={},
                     aliases={("vendor", "foo"): ("Some Variety", "Granite")},
                     payload=_TYPELESS)
    assert card["decided"] is True
    assert card["stone_type"] == "Granite"            # backfilled from the decision (was '')
    assert card["current_seed_type"] == "Granite"
    assert card["listings"] == [{"source": "vendor", "scraped": "Foo"}]   # identity fields untouched


def test_an_undecided_type_less_card_keeps_its_empty_type(monkeypatch):
    """Backfill is decided-only: an untouched type-less card stays type-less (there is no decision to key
    it on yet), so nothing is invented."""
    card = _one_card(monkeypatch, actions={}, aliases={}, payload=_TYPELESS)
    assert card["decided"] is False
    assert card["stone_type"] == ""
