"""A product Medusa holds that a later produce HOLDS (validate rejects it, stock_undetermined or a tree gap among
others, or keeps it for review) is HIDDEN: its stock is served as quantity 0 with reason 'held', so no buyer can order
a stock the scraper can no longer state. BlokPort already hides a stock that is not on sale (status rejected, as a
sell-out) and reads an unknown reason as a plain stock update, so the hide needs no change on its side. A held product
is not dead stock: the purge keeps it, and the produce that emits it again serves its real stock. The mass-delist
guard caps a held burst as it caps a delist (a broken scrape that holds most of a source must not hide it all), and the
run summary names the refusal. No em dashes."""
from __future__ import annotations

import logging
from types import SimpleNamespace

from stone_pipeline.config.sources import load_source
from stone_pipeline.core.manifest import Manifest
from stone_pipeline.core.schema import CanonicalRow, OutputMode
from stone_pipeline.ledger import sync, writethrough
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.ledger.populate import populate_inventory
from stone_pipeline.run import decide_delist, print_summary
from stone_pipeline.stages import product_state, validate

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


def _decide(held_n: int, live_n: int = 10):
    ns = [str(i) for i in range(live_n)]
    rows = [_row(n) for n in ns]
    validation = validate.ValidationResult(emit=rows[held_n:], rejects=rows[:held_n])
    ctx = SimpleNamespace(run_log=logging.LoggerAdapter(logging.getLogger("held-test"), {}), source_cfg=CFG,
                          scrape_path=None,
                          manifest=Manifest(run_id="r", source="marenostone", code_version="x", environment="dev"))
    return ctx, decide_delist(ctx, rows, validation, _known(*ns))


def test_the_mass_delist_guard_caps_a_held_burst_and_the_summary_names_it(capsys):
    ctx, delist = _decide(held_n=2)                                   # 2 of 10 live: hidden
    assert [sku for sku, _ in delist.held] == [_sku("0"), _sku("1")]
    assert ctx.manifest.totals["products_held_hidden"] == 2
    ctx, delist = _decide(held_n=5)                                   # 5 of 10 live: a broken scrape, refused
    assert delist.held == []
    assert (ctx.manifest.totals["products_held_hidden"], ctx.manifest.totals["held_hide_refused"]) == (0, 5)
    print_summary(ctx.manifest)
    assert "held hidden       0 (refused 5" in capsys.readouterr().out
