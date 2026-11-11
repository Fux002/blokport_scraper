"""A product Medusa holds that a later produce HOLDS (validate rejects it, stock_undetermined or a tree gap among
others, or keeps it for review) is HIDDEN: its stock is served as quantity 0 with reason 'held', so no buyer can order
a stock the scraper can no longer state. BlokPort already hides a stock that is not on sale (status rejected, as a
sell-out) and reads an unknown reason as a plain stock update, so the hide needs no change on its side. A held product
is not dead stock: the purge keeps it, and the produce that emits it again serves its real stock. No em dashes."""
from __future__ import annotations

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.schema import CanonicalRow, OutputMode
from stone_pipeline.ledger import sync, writethrough
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_inventory
from stone_pipeline.stages import product_state

CFG = load_source("marenostone")


def _row(n: str, qty: int | None = 5) -> CanonicalRow:
    return CanonicalRow(src_site="marenostone", surrogate_key=n, inventory_quantity=qty)


def _sku(n: str) -> str:
    return f"{CFG.source_code}-{n}".upper()


def _known(*ns: str) -> product_state.KnownProducts:
    return product_state.KnownProducts(by_sku={_sku(n): {"handle": f"h-{n}", "inventory": "6"} for n in ns})


def _ledger_with_a_published_product(path, n="1"):
    """MAR-<n>, in Medusa, its stock 6 synced on an earlier produce (dev SEN-7134: qty 6 since 11-04)."""
    with Ledger.open(path, env=writethrough.ENV_NAME, backend_id_fingerprint=writethrough.backend_fingerprint()) as lg:
        now = now_iso()
        lg.execute("INSERT INTO product (sku, source, state, medusa_id, last_synced, created_at, updated_at) "
                   "VALUES (?, ?, 'synced', 'prod_1', ?, ?, ?)", (_sku(n), CFG.source_code.lower(), now, now, now))
        lg.execute("INSERT INTO inventory (sku, qty, made_to_order, last_synced_qty, last_synced_made_to_order, "
                   "updated_at) VALUES (?, 6, 0, 6, 0, ?)", (_sku(n), now))


def _record(path, held):
    assert writethrough.record_source([], (), CFG, mode=OutputMode.FULL, held=held, path=path)


def _served(path):
    with Ledger.open(path, env=writethrough.ENV_NAME) as lg:
        return sync.ready_inventory(lg)


def _writethrough_on(monkeypatch):
    monkeypatch.delenv("BLOKPORT_LEDGER_WRITETHROUGH", raising=False)
    monkeypatch.setenv("SCRAPER_LEDGER_WRITETHROUGH", "1")


def test_a_published_product_held_on_the_next_produce_is_served_hidden(tmp_path, monkeypatch):
    _writethrough_on(monkeypatch)
    path = tmp_path / "dev.ledger"
    _ledger_with_a_published_product(path)
    _record(path, [(_sku("1"), "h-1")])
    assert _served(path) == [{"external_id": _sku("1"),
                              "payload": {"sku": _sku("1"), "quantity": 0, "made_to_order": False, "reason": "held"}}]


def test_a_held_product_medusa_never_had_is_not_hidden():
    # only a product Medusa holds (the products export) has a stock to take down; a new one simply never lists
    held = product_state.held([_row("1"), _row("9")], CFG, _known("1"))
    assert held == [(_sku("1"), "h-1")]


def test_the_purge_keeps_a_held_product(tmp_path, monkeypatch):
    _writethrough_on(monkeypatch)
    path = tmp_path / "dev.ledger"
    _ledger_with_a_published_product(path)
    _record(path, [(_sku("1"), "h-1")])
    with Ledger.open(path, env=writethrough.ENV_NAME) as lg:
        assert sync.purge_discontinued(lg) == {"product": 0, "inventory": 0, "external_ids": []}
        assert lg.get("product", "sku", _sku("1")) is not None


def test_a_held_product_emitted_again_serves_its_real_stock(tmp_path, monkeypatch):
    _writethrough_on(monkeypatch)
    path = tmp_path / "dev.ledger"
    _ledger_with_a_published_product(path)
    _record(path, [(_sku("1"), "h-1")])
    with Ledger.open(path, env=writethrough.ENV_NAME) as lg:
        assert sync.ack(lg, "inventory", _sku("1"), status_="updated", quantity=0, made_to_order=False) == 1
        assert sync.ready_inventory(lg) == []                       # the hide is synced
        populate_inventory(lg, [_row("1", qty=5)], CFG)             # the next produce emits it again
        assert sync.ready_inventory(lg) == [{"external_id": _sku("1"),
                                             "payload": {"sku": _sku("1"), "quantity": 5, "made_to_order": False}}]


def _decide(rows, held_rows, known, caplog):
    import logging
    from types import SimpleNamespace
    from stone_pipeline import run as run_module
    from stone_pipeline.stages.validate import ValidationResult
    ctx = SimpleNamespace(run_log=logging.LoggerAdapter(logging.getLogger("held-guard"), {}), source_cfg=CFG,
                          scrape_path=None, manifest=SimpleNamespace(totals={}))
    with caplog.at_level("WARNING"):
        return run_module.decide_delist(ctx, rows, ValidationResult(rejects=held_rows), known)


def test_a_run_that_holds_too_much_of_the_live_catalogue_hides_none_and_says_so(caplog):
    # a broken stage that rejects every row must not take the whole catalogue off sale: the hide passes the SAME
    # mass-delist guard as a delist (delist_max_fraction of the live catalogue), counted together with it
    rows = [_row(str(i)) for i in range(10)]
    delist = _decide(rows, rows, _known(*(str(i) for i in range(10))), caplog)
    assert delist.held == [] and delist.discontinued == []
    assert any("refused" in r.getMessage() and "hid" in r.getMessage() for r in caplog.records)


def test_a_run_that_holds_a_few_live_products_hides_them(caplog):
    rows = [_row(str(i)) for i in range(10)]
    delist = _decide(rows, rows[:2], _known(*(str(i) for i in range(10))), caplog)
    assert delist.held == [(_sku("0"), "h-0"), (_sku("1"), "h-1")]
    assert not any("refused" in r.getMessage() for r in caplog.records)


def test_an_unrelated_hold_never_blocks_a_real_delist(caplog):
    # dev varsha's shape (11-11): 13 live, 2 held, 3 really dropped by the supplier. Each stock-off action passes the
    # guard on its own count (2/13 and 3/13 are under the fraction), so the hold never refuses the delist.
    rows = [_row(str(i)) for i in range(10)]                     # the scrape: 0..9; 10, 11 and 12 are gone
    delist = _decide(rows, rows[:2], _known(*(str(i) for i in range(13))), caplog)
    assert delist.held == [(_sku("0"), "h-0"), (_sku("1"), "h-1")]
    assert sorted(sku for sku, _ in delist.discontinued) == [_sku("10"), _sku("11"), _sku("12")]
    assert not any("refused" in r.getMessage() for r in caplog.records)
