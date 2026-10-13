"""Lot sold answers: the operator's answer to a lot's "which slabs are sold?" card (stages/lots.cards).

An answer names the scanned slab positions that are sold and is stored for the stock figure the card asked
at; the loader applies it only while that figure still holds, so a later sale asks again. It is checked
against the card it answers: only scanned slabs, each once, and never leaving more slabs on offer than the
vendor has in stock."""

from __future__ import annotations

import json
from contextlib import closing

from stone_pipeline.config import store

from ._common import InvalidDecision, _now


def answer_lot(ref: str, sold: list) -> list[int]:
    """Record the sold slabs for the pending lot card `ref` ("<source>|<lot ref>"). Returns them sorted.
    LookupError when there is no such card; InvalidDecision when the answer contradicts it."""
    from .queue import pending_payload          # the card is the authority on what may be answered
    card = pending_payload("lot", ref)
    if card is None:
        raise LookupError(f"no pending lot card {ref!r}")
    if not isinstance(sold, list) or not all(type(n) is int for n in sold):
        raise InvalidDecision("sold must be a list of slab positions (whole numbers)")
    if len(set(sold)) != len(sold):
        raise InvalidDecision("a slab is marked sold twice")
    scanned = {p["position"] for p in card["pieces"]}
    stray = sorted(set(sold) - scanned)
    if stray:
        raise InvalidDecision(f"{stray} is not a scanned slab of this lot")
    on_offer = len(scanned) - len(sold)
    if on_offer > card["available"]:
        raise InvalidDecision(f"{on_offer} slabs would stay on offer, but the vendor has {card['available']} "
                              f"slabs in stock: mark {on_offer - card['available']} more as sold")
    with closing(store.open_store()) as conn:
        conn.execute("INSERT INTO lot_sold_answer (source, lot_ref, available, sold, decided_at) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, lot_ref) DO UPDATE SET "
                     "available = excluded.available, sold = excluded.sold, decided_at = excluded.decided_at",
                     (card["source"], card["lot_ref"], card["available"], json.dumps(sorted(sold)), _now()))
        conn.commit()
    return sorted(sold)


def lot_answers(source: str) -> dict[str, tuple[int, frozenset[int]]]:
    """lot ref -> (stock figure answered at, sold positions) for one source. Empty for a fresh store."""
    with closing(store.read_store()) as conn:
        rows = conn.execute("SELECT lot_ref, available, sold FROM lot_sold_answer WHERE source = ?",
                            (source,)).fetchall()
    return {r["lot_ref"]: (r["available"], frozenset(json.loads(r["sold"]))) for r in rows}
