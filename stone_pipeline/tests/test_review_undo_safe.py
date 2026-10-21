"""Undo (DELETE /config/v1/review/decide) is real and safe.

Two defects, both found tracing the 2026-10 dev first-of-type cleanup:
  * the request body was read only for PUT/POST, so every DELETE over HTTP reached the handler with no body
    and returned 400: the admin's undo had never worked. Tests called dispatch() directly, which hid it.
  * undoing a decision the last produce APPLIED can leave that listing's products held on the next produce,
    and a held product is never written: it stays live in Medusa under the old variety with frozen stock.
    So an applied decision that bound products of its listing is refused (restate it instead); a decision made
    after the last produce, or one that bound nothing, can still be undone.

Pure tests: the autouse conftest fixture isolates config.db; produce outputs live in a tmp dir.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import polars as pl

from stone_pipeline.config import decisions_store, resolved, server, store
from stone_pipeline.config.server import ConfigHandler

_PAST = "2000-01-01T00:00:00+00:00"
_FUTURE = "2999-01-01T00:00:00+00:00"
_COLS = list(resolved._COLUMNS)


def _bind(source: str, spelling: str, name: str = "Royal Black", stone_type: str = "Marble") -> None:
    decisions_store.decide(source, spelling, name, stone_type,
                           exists_as=lambda n, t: True, alias_target=lambda n, t: None)


def _produced(finished_at: str, status: str = "succeeded", stage: str = "republish") -> None:
    store.record_run_log({"run_id": f"r{finished_at}{stage}", "finished_at": finished_at,
                          "status": status, "stage": stage})


def _outputs(monkeypatch, tmp_path, rows: list[dict]):
    d = tmp_path / "outputs" / "marenostone_20261021_000000" / "diagnostics"
    d.mkdir(parents=True)
    if rows:
        pl.DataFrame([{c: r.get(c, "") for c in _COLS} for r in rows]).write_parquet(d / "canonical.parquet")
    monkeypatch.setattr(resolved, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(outputs_dir=tmp_path / "outputs")))


def _row(key: str, name: str, variation_key: str = "slab_marble_royal_black_x", match_key: str | None = None) -> dict:
    return {"src_site": "marenostone", "surrogate_key": key, "raw_name": name,
            "variety_match_key": name if match_key is None else match_key,
            "variation_key": variation_key, "variation_name": "Royal Black", "type_name": "Marble"}


def _undo(*listings: tuple[str, str]):
    return server.dispatch("DELETE", ["review", "decide"],
                           {"listings": [{"source": s, "scraped": sp} for s, sp in listings]})


def _standing(source: str, spelling: str) -> bool:
    return decisions_store.statement_decided_at(source, spelling) is not None


# -- the HTTP body ---------------------------------------------------------------------------------------

def test_a_delete_body_is_read_over_http():
    _bind("marenostone", "Black Marble")
    raw = json.dumps({"listings": [{"source": "marenostone", "scraped": "Black Marble"}]}).encode()
    h = ConfigHandler.__new__(ConfigHandler)          # the real request path, without opening a socket
    h.headers = {"Authorization": "Bearer t", "Content-Length": str(len(raw))}
    h.server = SimpleNamespace(expected_token="t")
    h.path, h.rfile = "/config/v1/review/decide", io.BytesIO(raw)
    replies: list = []
    h._respond = lambda code, payload: replies.append((code, payload))
    h._handle("DELETE")
    assert replies[0][0] == 200, replies
    assert not _standing("marenostone", "Black Marble")


# -- the live-product guard ------------------------------------------------------------------------------

def test_undo_before_the_next_produce_is_allowed_even_with_bound_products(monkeypatch, tmp_path):
    _produced(_PAST)                                  # the last produce ran BEFORE this decision
    _bind("marenostone", "Black Marble")
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble")])
    code, body = _undo(("marenostone", "Black Marble"))
    assert code == 200 and body["cleared"] == [{"statements": 1}]
    assert not _standing("marenostone", "Black Marble")


def test_undo_of_an_applied_decision_that_bound_products_is_refused(monkeypatch, tmp_path):
    _bind("marenostone", "Black Marble")
    _produced(_FUTURE)                                # the last produce ran AFTER the decision: applied
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble"), _row("2", "Black Marble")])
    code, body = _undo(("marenostone", "Black Marble"))
    assert code == 409
    assert body["blocked"] == [{"source": "marenostone", "scraped": "Black Marble", "products": 2}]
    assert _standing("marenostone", "Black Marble")   # nothing cleared


def test_undo_of_an_applied_decision_that_bound_nothing_is_allowed(monkeypatch, tmp_path):
    _bind("marenostone", "Black Marble")
    _produced(_FUTURE)
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble", variation_key=""),   # held, not bound
                                     _row("2", "White Marble")])                     # another listing
    code, _ = _undo(("marenostone", "Black Marble"))
    assert code == 200 and not _standing("marenostone", "Black Marble")


def test_a_bound_product_matched_by_its_raw_name_also_blocks(monkeypatch, tmp_path):
    _bind("marenostone", "Black Marble Slab")
    _produced(_FUTURE)
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble Slab", match_key="Black Marble")])
    code, body = _undo(("marenostone", "Black Marble Slab"))
    assert code == 409 and body["blocked"][0]["products"] == 1


def test_undo_of_an_applied_decision_without_produce_output_is_refused(monkeypatch, tmp_path):
    # the decision was applied but this host has no produce output to check against: unverifiable, so refuse
    _bind("marenostone", "Black Marble")
    _produced(_FUTURE)
    _outputs(monkeypatch, tmp_path, [])
    code, body = _undo(("marenostone", "Black Marble"))
    assert code == 409 and "cannot verify" in body["error"]
    assert _standing("marenostone", "Black Marble")


def test_with_only_failed_or_scrape_only_runs_on_record_the_output_decides(monkeypatch, tmp_path):
    # neither run applied a decision, so neither proves this one is still unapplied: bound products block
    _bind("marenostone", "Black Marble")
    _produced(_FUTURE, status="failed")
    _produced(_FUTURE, stage="scrape")
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble")])
    code, _ = _undo(("marenostone", "Black Marble"))
    assert code == 409


def test_a_multi_listing_undo_is_all_or_nothing(monkeypatch, tmp_path):
    _bind("marenostone", "Black Marble")
    _bind("marenostone", "White Marble", name="Azna White")
    _produced(_FUTURE)
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble")])        # only Black Marble bound products
    code, body = _undo(("marenostone", "Black Marble"), ("marenostone", "White Marble"))
    assert code == 409 and [b["scraped"] for b in body["blocked"]] == ["Black Marble"]
    assert _standing("marenostone", "Black Marble") and _standing("marenostone", "White Marble")


def test_an_applying_run_rolled_out_of_the_log_still_blocks(monkeypatch, tmp_path):
    # run_log keeps the newest 50 runs: after many inventory runs the applying produce is gone from it, so
    # "no applied run on record" must not be read as "nothing applied" while products are bound
    _bind("marenostone", "Black Marble")
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble")])
    code, body = _undo(("marenostone", "Black Marble"))
    assert code == 409 and body["blocked"][0]["products"] == 1


def test_nothing_produced_and_no_output_allows_undo(monkeypatch, tmp_path):
    _bind("marenostone", "Black Marble")
    _outputs(monkeypatch, tmp_path, [])
    code, _ = _undo(("marenostone", "Black Marble"))
    assert code == 200 and not _standing("marenostone", "Black Marble")


def test_undo_of_a_listing_with_no_standing_statement_is_a_noop(monkeypatch, tmp_path):
    _produced(_FUTURE)
    _outputs(monkeypatch, tmp_path, [_row("1", "Black Marble")])
    code, body = _undo(("marenostone", "Black Marble"))
    assert code == 200 and body["cleared"] == [{"statements": 0}]


# -- the shared "last applied produce" definition ---------------------------------------------------------

def test_last_applied_produce_is_the_newest_successful_catalog_run():
    assert store.last_applied_produce_at() is None
    _produced(_PAST)
    _produced(_FUTURE, status="failed")
    _produced(_FUTURE, stage="inventory")
    assert store.last_applied_produce_at() == _PAST
