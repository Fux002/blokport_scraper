"""The learned-alias review read (P2-10 C6, PHASE-2-SPEC WP2.3 "Review API"): what GET /config/v1/review/aliases
serves to the P2-11 AliasesPanel, built from config.db `learned_alias` (C2), the ledger's varieties and the cleanup's
open items (C5's aliases.open_items).

A card is one learned row with its evidence: the value and its norm, the variety it attaches to (name and type, read
from the ledger by the row's identity Key; null when the ledger no longer holds that Key, never guessed), the vendor
that proposed it, the method and score, and the decision. Its id is "<identity_key>|<norm>": match_key folds every
non-alphanumeric character to a space, so a norm never holds the separator.
"""

from __future__ import annotations

import json

from stone_pipeline.aliases.open_items import OPEN_FILE, read_open_items
from stone_pipeline.config import decisions_store
from stone_pipeline.config.settings import SETTINGS

ID_SEPARATOR = "|"


class LedgerUnavailable(RuntimeError):
    """The ledger the cards' varieties are read from is absent: named, never served as cards without a variety."""


def card_id(identity_key: str, norm: str) -> str:
    return f"{identity_key}{ID_SEPARATOR}{norm}"


def parse_id(raw: str) -> tuple[str, str]:
    """(identity_key, norm) from a card id; ValueError when either half is empty or the separator is missing."""
    key, sep, norm = raw.partition(ID_SEPARATOR)
    if not sep or not key.strip() or not norm.strip():
        raise ValueError(f"an alias id is '<identity_key>{ID_SEPARATOR}<norm>', got {raw!r}")
    return key, norm


def _varieties(keys: list[str]) -> dict[str, tuple[str, str]]:
    """Key -> (name, type) from the ledger, in one read. An absent ledger raises LedgerUnavailable."""
    from stone_pipeline.ledger import writethrough
    try:
        with writethrough.connect(bind=False) as lg:
            return {r["key"]: (r["name"], r["type"]) for r in lg.execute(
                "SELECT key, name, type FROM variation WHERE key IN (SELECT value FROM json_each(?))",
                (json.dumps(keys),))}
    except FileNotFoundError as exc:
        raise LedgerUnavailable(str(exc)) from exc


def _card(row: dict, names: dict[str, tuple[str, str]]) -> dict:
    variety, stone_type = names.get(row["identity_key"], (None, None))
    return {"id": card_id(row["identity_key"], row["norm"]), "identity_key": row["identity_key"],
            "variety": variety, "stone_type": stone_type, "value": row["value"], "norm": row["norm"],
            "method": row["method"], "source": row["source"], "score": row["score"], "status": row["status"],
            "first_seen": row["first_seen"], "decided_by": row["decided_by"], "decided_at": row["decided_at"]}


def review(status: str | None = None) -> dict:
    """The review page's one read: the cards (every learned alias, or every one of `status`, sorted by (identity_key,
    norm)), the count per status over ALL rows, and the cleanup's open items with how they were read: 'current' (the
    file) or 'absent' (no cleanup has written one on this host yet, which is not "nothing open"). `status` is one of
    LEARNED_STATUSES or None (the caller validates it); a malformed open file raises OpenItemsUnreadable; an absent
    ledger, LedgerUnavailable."""
    rows = decisions_store.list_learned_aliases()
    shown = [r for r in rows if status is None or r["status"] == status]
    names = _varieties(sorted({r["identity_key"] for r in shown})) if shown else {}
    counts = dict.fromkeys(decisions_store.LEARNED_STATUSES, 0)
    for r in rows:
        counts[r["status"]] += 1
    items = read_open_items(SETTINGS.paths.review_dir / OPEN_FILE)
    return {"aliases": [_card(r, names) for r in shown], "counts": counts,
            "open": items if items is not None else [], "open_read": "current" if items is not None else "absent"}
