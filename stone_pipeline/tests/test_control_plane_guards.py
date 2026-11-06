"""The /config/v1 control plane refuses what would silently undo a lifecycle decision or corrupt a store:
an explicit run of a delisted source, a source PUT that flips `enabled` past pause/resume/delist, a source_code
change under live ledger products, a multi-listing statement that half-commits, and bodies of the wrong type
(each a clean 400 naming the field, never a 500 or a silent coercion)."""

from __future__ import annotations

import pytest

from stone_pipeline import lifecycle
from stone_pipeline.config import decisions_store, runner, server, store, varieties
from stone_pipeline.reference.loaders import _norm


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    runner._runs.clear()
    runner._current_id = None
    lifecycle._active = None
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    monkeypatch.setenv("BLOKPORT_LEDGER_PATH", str(tmp_path / "dev.ledger"))
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("polonine:\n  source_code: pol\n  vendor: P\n  ports_default:\n    - Brindisi\n"
                         "varsha:\n  source_code: var\n  vendor: V\n  ports_default:\n    - Brindisi\n",
                         encoding="utf-8")
    store.seed_from_yaml(yaml_path=yaml_path)
    yield
    runner._runs.clear()
    runner._current_id = None
    lifecycle._active = None


def _finish(rec):
    rec["status"] = "succeeded"


# -- 1: an explicit-scope run never scrapes a source the lifecycle took out --------------------------------

@pytest.mark.parametrize("stage", ["scrape", "inventory", "all", "republish"])
def test_an_explicit_run_refuses_a_delisted_or_paused_source(stage):
    store.set_state("polonine", lifecycle="delisted", enabled=False)
    store.set_state("varsha", lifecycle="paused", enabled=False)
    for name in ("polonine", "varsha"):
        rec, code = runner.start_run(sources=[name], stage=stage, launch=_finish)
        assert code == 409 and name in rec["error"] and "resume" in rec["error"]
        assert runner._current_id is None                              # no run claimed
    store.set_state("varsha", lifecycle="active", enabled=True)
    assert runner.start_run(sources=["varsha"], stage=stage, launch=_finish)[1] == 202


def test_the_run_route_refuses_a_delisted_source():
    store.set_state("polonine", lifecycle="delisted", enabled=False)
    code, body = server.dispatch("POST", ["run"], {"sources": ["polonine"], "stage": "scrape"})
    assert code == 409 and "polonine" in body["error"] and "delisted" in body["error"]


@pytest.mark.parametrize("lifecycle", [None, "active"])
def test_a_disabled_row_without_a_lifecycle_reads_disabled_and_is_refused_by_the_same_name(lifecycle):
    # legacy rows: enabled 0 with lifecycle NULL, or 'active' (a settings PUT used to flip enabled alone). The listing
    # and the run guard read ONE derivation of the state, so the list cannot call "active" what /run refuses, and
    # the guard refuses what `all` (enabled only) would never run
    store.set_state("varsha", lifecycle=lifecycle, enabled=False)
    assert store.get_row("varsha")["lifecycle"] == "disabled"
    code, body = server.dispatch("GET", ["sources"], None)
    assert {r["source"]: r["lifecycle"] for r in body["sources"]} == {"polonine": "active", "varsha": "disabled"}
    code, body = server.dispatch("POST", ["run"], {"sources": ["varsha"], "stage": "scrape"})
    assert code == 409 and "varsha (disabled)" in body["error"]


# -- 2: `enabled` is owned by pause / resume / delist ---------------------------------------------------------

def _source_body(**over):
    return {"source_code": "var", "vendor": "V", "ports": ["Brindisi"], **over}


def test_a_stale_form_cannot_re_enable_a_delisted_source():
    store.set_state("varsha", lifecycle="delisted", enabled=False)
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(enabled=True))
    assert code == 409 and "/resume" in body["error"]
    row = store.get_row("varsha")
    assert row["enabled"] is False and row["lifecycle"] == "delisted"
    assert store.enabled_names() == {"polonine"}


def test_the_form_round_trip_keeps_the_lifecycle_state():
    # the admin form sends `enabled` back at its stored value with an edited setting: accepted, state unchanged
    store.set_state("varsha", lifecycle="delisted", enabled=False)
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(enabled=False, vendor="Renamed"))
    assert code == 200 and body["vendor"] == "Renamed"
    assert body["enabled"] is False and body["lifecycle"] == "delisted"


def test_a_put_without_enabled_keeps_the_stored_state():
    store.set_state("varsha", lifecycle="paused", enabled=False)
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(vendor="Renamed"))
    assert code == 200 and body["enabled"] is False and body["lifecycle"] == "paused"


def test_a_put_cannot_change_the_lifecycle_label():
    store.set_state("varsha", lifecycle="delisted", enabled=False)
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(lifecycle="active"))
    assert code == 409 and "lifecycle" in body["error"]
    assert store.get_row("varsha")["lifecycle"] == "delisted"


def test_a_new_source_starts_active():
    store.delete_source("varsha")
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(enabled=False))
    assert code == 409 and "/pause" in body["error"]
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(enabled=True))
    assert code == 200 and body["enabled"] is True and body["lifecycle"] == "active"


# -- 3: the source_code is the SKU prefix of every product the ledger holds -------------------------------------

def _ledger_product(source_code: str) -> None:
    from stone_pipeline.ledger import writethrough
    from stone_pipeline.ledger.db import Ledger, now_iso
    with Ledger.open(writethrough.ledger_path(), env=writethrough.ENV_NAME) as lg:
        now = now_iso()
        lg.upsert("variation", {"key": "slab_v1", "branch": "slab", "type": "Marble", "name": "x",
                                "aliases": "[]", "image_url": "u", "image_sha256": None, "image_model": None,
                                "volume": "", "medusa_id": None, "in_full": 1, "payload_hash": "",
                                "state": "synced", "first_seen": now, "last_synced": now,
                                "created_at": now, "updated_at": now}, pk=("key",))
        lg.upsert("product", {"sku": f"{source_code.upper()}-1", "source": source_code, "variation_key": "slab_v1",
                              "medusa_id": "PID", "state": "synced", "created_at": now, "updated_at": now},
                  pk=("sku",))


def test_a_source_code_change_under_ledger_products_is_refused():
    _ledger_product("var")
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(source_code="vax"))
    assert code == 409 and "'var'" in body["error"] and "1 product" in body["error"]
    assert store.get_row("varsha")["source_code"] == "var"


@pytest.mark.parametrize("sent", ["VAR", " var ", ""])
def test_a_code_that_normalises_to_the_stored_one_is_not_a_change(sent):
    # the store lowercases and strips the code and backfills an empty one from the name (SourceConfig)
    _ledger_product("var")
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(source_code=sent))
    assert code == 200 and body["source_code"] == "var"


def test_a_source_code_change_without_products_is_allowed():
    _ledger_product("var")                                             # another source's products only
    code, body = server.dispatch("PUT", ["sources", "polonine"],
                                 {"source_code": "plx", "vendor": "P", "ports": ["Brindisi"]})
    assert code == 200 and body["source_code"] == "plx"


# -- 7: one statement over several listings is all or nothing ---------------------------------------------------

def test_a_multi_listing_statement_that_fails_on_one_listing_stores_none(monkeypatch):
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: False)
    monkeypatch.setattr(varieties, "alias_target", lambda n, t: None)
    monkeypatch.setattr(server, "_type_vocab", lambda: {"granite": "Granite"})
    code, body = server.dispatch("PUT", ["review", "decide"], {
        "listings": [{"source": "varsha", "scraped": "Bonsai Green"}, {"source": "polonine", "scraped": "東京"}],
        "name": "Bonsai Green", "type": "Granite"})
    assert code == 400
    assert decisions_store._statement_rows() == []                     # listing 1 was not stored either


def test_a_multi_listing_statement_stores_every_listing(monkeypatch):
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: False)
    monkeypatch.setattr(varieties, "alias_target", lambda n, t: None)
    monkeypatch.setattr(server, "_type_vocab", lambda: {"granite": "Granite"})
    code, body = server.dispatch("PUT", ["review", "decide"], {
        "listings": [{"source": "varsha", "scraped": "Bonsai Green"}, {"source": "polonine", "scraped": "Bonsai Green"}],
        "name": "Bonsai Green", "type": "Granite"})
    assert code == 200 and body["decided"] == 2
    # the first listing defines the spelling's global meaning; the second restates it (sequential, one transaction)
    assert [listing["result"] for listing in body["listings"]] == ["minted", "minted"]
    assert {(r["source"], r["spelling_norm"]) for r in decisions_store._statement_rows()} == {("", _norm("Bonsai Green"))}


def test_an_undo_over_several_listings_with_an_empty_key_spelling_clears_none(monkeypatch):
    # a spelling that folds to no key is a 400 naming it, checked before anything is cleared: the listings before it
    # keep their statements (it used to be a 500 after they were already cleared)
    monkeypatch.setattr(varieties, "exists_as", lambda n, t: False)
    monkeypatch.setattr(varieties, "alias_target", lambda n, t: None)
    decisions_store.decide("varsha", "Bonsai Green", "Bonsai Green", "Granite")
    decisions_store.decide("polonine", "Other Stone", "Other Stone", "Granite")
    before = decisions_store._statement_rows()
    code, body = server.dispatch("DELETE", ["review", "decide"], {
        "listings": [{"source": "varsha", "scraped": "Bonsai Green"}, {"source": "polonine", "scraped": "Other Stone"},
                     {"source": "polonine", "scraped": "東京"}]})
    assert code == 400 and "東京" in body["error"]
    assert decisions_store._statement_rows() == before


def test_an_undo_over_several_listings_clears_them_all():
    for source, spelling in (("varsha", "Bonsai Green"), ("polonine", "Other Stone")):
        decisions_store.reject(source, spelling)
    code, body = server.dispatch("DELETE", ["review", "decide"], {
        "listings": [{"source": "varsha", "scraped": "Bonsai Green"}, {"source": "polonine", "scraped": "Other Stone"}]})
    assert code == 200 and body["cleared"] == [{"statements": 1}, {"statements": 1}]
    assert decisions_store._statement_rows() == []


# -- 8: bodies are type-checked at the boundary ------------------------------------------------------------------

_DECIDE = {"source": "varsha", "scraped": "Bonsai", "name": "Bonsai", "type": "Granite"}


@pytest.mark.parametrize("method, path, body, field", [
    ("POST", ["run"], {"sources": 5}, "sources"),
    ("POST", ["run"], {"sources": []}, "sources"),
    ("POST", ["run"], {"sources": "polonine"}, "sources"),
    ("POST", ["run"], {"sources": [5]}, "sources"),
    ("POST", ["run"], {"stage": 5}, "stage"),
    ("POST", ["run"], ["polonine"], "body"),
    ("POST", ["purge"], {"sources": []}, "sources"),
    ("POST", ["clean"], {"sources": []}, "sources"),
    ("POST", ["delist"], {"sources": 5}, "sources"),
    ("POST", ["reset"], {"hard": "false"}, "hard"),
    ("POST", ["reset"], {"pristine": 1}, "pristine"),
    ("POST", ["variations", "unmint"], {"all_minted": "yes"}, "all_minted"),
    ("POST", ["variations", "slab_x", "retire"], {"force": "true"}, "force"),
    ("PUT", ["review", "decide"], {**_DECIDE, "source": 5}, "source"),
    ("PUT", ["review", "decide"], {**_DECIDE, "scraped": [5]}, "scraped"),
    ("PUT", ["review", "decide"], {**_DECIDE, "name": 5}, "name"),
    ("PUT", ["review", "decide"], {**_DECIDE, "type": ["Granite"]}, "type"),
    ("PUT", ["review", "decide"], {**_DECIDE, "color": 3}, "color"),
    ("PUT", ["review", "decide"], {**_DECIDE, "origin": 3}, "origin"),
    ("PUT", ["review", "decide"], {**_DECIDE, "widen": "yes"}, "widen"),
    ("PUT", ["review", "decide"], {**_DECIDE, "listings": "varsha"}, "listings"),
    ("PUT", ["review", "decide"], {**_DECIDE, "listings": [{"source": 5, "scraped": "x"}]}, "listings"),
    ("DELETE", ["review", "decide"], {**_DECIDE, "listings": ["varsha"]}, "listings"),
    ("PUT", ["review", "attributes", "white"], {"kind": 5, "medusa_id": "x"}, "kind"),
    ("PUT", ["review", "backbone", "a|b|color|c"], {"action": 5}, "action"),
    ("POST", ["review", "backbone", "approve_all"], {"verdict": 5}, "verdict"),
    ("PUT", ["origins", "x"], {"variety": "A", "stone_type": "Granite", "countries": [5]}, "countries"),
    ("PUT", ["origins", "x"], {"variety": "A", "stone_type": "Granite", "countries": "IT"}, "countries"),
    ("PUT", ["origins", "x"], {"variety": 5, "stone_type": "Granite", "countries": ["IT"]}, "variety"),
    ("PUT", ["sources", "varsha"], _source_body(mode="garbage"), "mode"),
    ("PUT", ["sources", "varsha"], _source_body(image_pipeline="false"), "image_pipeline"),
    ("PUT", ["sources", "varsha"], _source_body(emit_on_review=0), "emit_on_review"),
    ("PUT", ["sources", "varsha"], _source_body(enabled="false"), "enabled"),
    ("PUT", ["sources", "varsha"], _source_body(vendor=5), "vendor"),
    ("PUT", ["sources", "varsha"], _source_body(source_code=7), "source_code"),
    ("PUT", ["sources", "varsha"], _source_body(ports=[5]), "ports"),
])
def test_a_wrongly_typed_body_is_a_400_naming_the_field(method, path, body, field):
    before = store.get_row("varsha")
    code, out = server.dispatch(method, path, body)
    assert code == 400 and field in out["error"], out
    assert store.get_row("varsha") == before                           # nothing was written


def test_a_null_text_setting_reads_as_empty():
    # the admin's new-source form sends company_id / primary_origin / collection_* as null: the column default, not
    # a NOT NULL failure misreported as a duplicate source_code
    store.delete_source("varsha")
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(
        enabled=True, company_id=None, primary_origin=None, collection_country=None, collection_city=None))
    assert code == 200 and body["company_id"] == "" and body["collection_city"] == ""


def test_a_null_integer_setting_is_a_400():
    code, body = server.dispatch("PUT", ["sources", "varsha"], _source_body(default_bundle_size=None))
    assert code == 400 and "default_bundle_size" in body["error"]


def test_a_valid_mode_and_strict_bools_are_accepted():
    code, body = server.dispatch("PUT", ["sources", "varsha"],
                                 _source_body(mode="auto", image_pipeline=False, emit_on_review=True))
    assert code == 200 and body["mode"] == "auto" and body["image_pipeline"] is False


# -- 10: the server checks its token before touching any state; the 404 names the real routes -------------------

def test_serve_refuses_before_boot_when_no_token_is_set(monkeypatch):
    for var in ("SCRAPER_CONFIG_TOKEN", "BLOKPORT_CONFIG_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def _boot(*a, **k):
        raise AssertionError("boot (restore, seed, reconcile) ran before the token check")
    monkeypatch.setattr(server, "boot", _boot)
    with pytest.raises(SystemExit, match="CONFIG_TOKEN"):
        server.serve()


def test_the_not_found_answer_names_every_route():
    code, body = server.dispatch("GET", ["nonsense"], None)
    assert code == 404
    for method, pattern, _ in server._ROUTES:
        assert f"{method} /config/v1/{'/'.join(pattern)}" in body["error"]
