"""The pending review queue: what the last produce surfaced as still-undecided (varieties, attributes,
backbone-leaf, origin). Produce WRITES it (replace_pending) at the end of a run; the config API READS it
(list_pending) to serve the admin between runs. `_project_decision` overlays the operator's standing decision
onto a card so a between-runs decision shows as `current_*` until the next produce regenerates the queue."""

from __future__ import annotations

import json
from contextlib import closing

from stone_pipeline.config import store
from stone_pipeline.config.decisions_model import Decisions

from . import leaves
from ._common import InvalidDecision, _norm, _now
from .statements import scope_key

_PENDING_KINDS = ("variety", "attribute", "backbone_leaf", "origin", "decision_gap")


def replace_pending(kind: str, rows: list[dict]) -> None:
    """Wholly replace the pending queue for `kind` with this produce's undecided items. A decided item simply
    is not in `rows`, so it stops appearing. Each row must carry `ref` (the stable key) and `payload` (the
    JSON-able dict the UI renders); `sources` (list) is optional provenance."""
    if kind not in _PENDING_KINDS:
        raise InvalidDecision(f"pending kind must be one of {_PENDING_KINDS}, got {kind!r}")
    now = _now()
    # Two surfaced items can share a ref: norm(name) collides (e.g. 'Imperial White' typed granite AND
    # quartzite clean to the same name; 'Blue-Carara' vs 'Blue Carara' likewise). A decision is keyed by
    # that normalized name, so the two are ONE queue entry -- collapse them (keep last, matching the old
    # name-keyed dict-overwrite) instead of hitting the (kind, ref) primary key with a duplicate INSERT.
    deduped = {r["ref"]: r for r in rows}
    with closing(store.open_store()) as conn:
        conn.execute("DELETE FROM review_pending WHERE kind = ?", (kind,))
        conn.executemany(
            "INSERT INTO review_pending (kind, ref, payload, sources, updated_at) VALUES (?, ?, ?, ?, ?)",
            [(kind, r["ref"], json.dumps(r["payload"]),
              json.dumps(r["sources"]) if r.get("sources") is not None else None, now)
             for r in deduped.values()])
        conn.commit()


def clear_review_pending(kinds: tuple[str, ...] = _PENDING_KINDS) -> int:
    """Empty the review queue(s) so a GLOBAL clean-start reset gives an immediately blank slate, instead of
    the pre-reset queue lingering until the next produce recomputes (replace_pending) it. Returns the number
    of rows dropped. Global-only, like the ledger reset's shared base layer -- a scoped per-source reset
    leaves the queue (and the rest of config.db) alone."""
    for k in kinds:
        if k not in _PENDING_KINDS:
            raise InvalidDecision(f"pending kind must be one of {_PENDING_KINDS}, got {k!r}")
    marks = ",".join("?" * len(kinds))
    with closing(store.open_store()) as conn:
        n = conn.execute(f"DELETE FROM review_pending WHERE kind IN ({marks})", tuple(kinds)).rowcount
        conn.commit()
    return n


def pending_payload(kind: str, ref: str) -> dict | None:
    """The stored payload for one pending item, or None. Lets the API act on a queued suggestion by its
    ref without the client resending the display fields."""
    with closing(store.read_store()) as conn:
        r = conn.execute("SELECT payload FROM review_pending WHERE kind = ? AND ref = ?",
                         (kind, ref)).fetchone()
    return json.loads(r["payload"]) if r else None


def _project_decision(item: dict, dec: Decisions, with_statements: bool = True) -> None:
    """Overlay the operator's standing decision onto ONE pending card, in place. This is the single source of
    the `current_*` view every restatement card (variety AND origin) shows, so the two kinds derive their
    editable decision identically and cannot drift apart. It reads the one decisions object:

      * `dec.bindings`   -- the per-vendor bind {(nsrc, nspell): (target variety, target type)}; the card's
                            own (source, scraped) listings are the keys, with a single-vendor fallback;
      * `dec.statements` -- the mint/reject statement per scope key (a reject is keyed by the card ref);
                            `with_statements=False` for a kind that has none (origin), so the view is driven
                            by the bind alone.

    From those it sets decided, current_action, current_alias_of and current_seed_name/type/colour/country,
    backfills an empty scraped type from the decided type (so a decided card stays re-keyable), and resolves
    the per-decision widen (`dec.widened`, the "documented origin" checkbox) keyed on (source, target, type).
    The card always reflects the LAST decision: a per-vendor bind supersedes a stale mint that the
    source-scoped clear could not drop, unless that mint's own rename points at the same bind (mint+rename is
    one decision, still read as the mint)."""
    keys = [(_norm(l.get("source", "")), _norm(l.get("scraped", "")))
            for l in (item.get("listings") or []) if isinstance(l, dict)]
    # single-vendor fallback: a variety card carries the vendor as `src`, an origin card as `source` (and no
    # listings in its stored payload), so accept either as the (vendor, scraped) key the bind was stored under
    if item.get("scraped"):
        if item.get("src"):
            keys.append((_norm(item["src"]), _norm(item["scraped"])))
        if item.get("source"):
            keys.append((_norm(item["source"]), _norm(item["scraped"])))
    hit = next((dec.bindings[k] for k in keys if k in dec.bindings), None)       # (target variety, target type)
    # a mint/rename/reject is keyed on a scraped spelling (or the cleaned ref for a reject), never guaranteed
    # to equal the card ref, so match on every spelling the card carries
    spellings = {_norm(s) for s in (item.get("spellings") or []) if s}
    if item.get("scraped"):
        spellings.add(_norm(item["scraped"]))
    spellings |= {_norm(l.get("scraped", "")) for l in (item.get("listings") or [])
                  if isinstance(l, dict) and l.get("scraped")}
    st = None
    if with_statements:
        statements = dec.statements
        # a reject is keyed on the card's NAME (its ref is name|type for a typed card), so look it up by the name
        st = (next((statements[k] for k in keys if k in statements), None)               # the vendor's own level
              or statements.get(scope_key("", (item.get("variant") or item.get("ref") or "").split("|")[0]))
              or next((statements[scope_key("", s)] for s in spellings if scope_key("", s) in statements), None))
    if st is not None and st.is_mint and hit is not None and _norm(st.name or "") != _norm(hit[0]):
        st = None                                                    # stale mint superseded by the newer bind
    action = None if st is None else ("mint" if st.is_mint else "reject")
    item["decided"] = action is not None or hit is not None
    item["current_action"] = action or ("alias" if hit else None)
    item["current_alias_of"] = hit[0] if hit else None
    item["current_seed_color"] = st.color if st is not None and st.is_mint else None
    item["current_seed_type"] = (st.stone_type if st is not None and st.is_mint else None) or (hit[1] if hit else None) or None
    item["current_seed_country"] = st.origin_iso if st is not None and st.is_mint else None
    item["current_seed_name"] = st.name if st is not None and st.is_mint else None
    if item["decided"] and not item.get("stone_type") and item["current_seed_type"]:
        item["stone_type"] = item["current_seed_type"]              # re-keyable: fill an empty scraped type
    tgt_n = _norm(item.get("current_alias_of") or item.get("current_seed_name")
                  or item.get("variant") or item.get("variety", ""))
    tgt_t = _norm(item.get("current_seed_type") or item.get("stone_type", ""))
    srcs = {_norm(l.get("source", "")) for l in (item.get("listings") or []) if isinstance(l, dict)}
    if item.get("src"):
        srcs.add(_norm(item["src"]))
    if item.get("source"):
        srcs.add(_norm(item["source"]))
    doc = next((dec.widened[(s, tgt_n, tgt_t)] for s in srcs if (s, tgt_n, tgt_t) in dec.widened), None)
    item["documented_origin"] = doc
    item["widen"] = doc is not None


def list_pending(kind: str) -> list[dict]:
    """The pending items for `kind`, each = its payload plus `sources` and the `current_action` already
    recorded for it (so the UI can show a decision made between runs, applied on the next produce)."""
    # Match a decision to a card WITHOUT relying on the card ref. The ref is the CLEANED variety name (the
    # cleaner strips a trailing type word: "Amazon Green Granite" -> ref "amazon green"), but decide() keys
    # a statement on the SCRAPED spelling ("amazon green granite"). So the flag has to match on the
    # card's own scraped spellings, not its ref -- otherwise every card whose cleaner stripped a suffix
    # comes back undecided even though the bind is stored (the Gold-card bug). Reject is the exception: it
    # is keyed on the ref, so ref is kept as a candidate too.
    from stone_pipeline.config import decisions_store          # the facade, so a test that patches
    dec = decisions_store.load_decisions() if kind in ("variety", "origin") else Decisions.empty()  # is honored
    leaf_actions = leaves._leaf_actions_by_ref() if kind == "backbone_leaf" else {}
    # for origin, key the confirmed country by the SAME composite ref the queue uses, so a decision made
    # between runs shows as current_country until the next produce regenerates the queue (and drops it).
    origin_actions = ({f"{s}|{v}|{t}": iso for (s, v, t), iso in dec.vendor_origins.items()}
                      if kind == "origin" else {})
    with closing(store.read_store()) as conn:
        rows = conn.execute(
            "SELECT ref, payload, sources FROM review_pending WHERE kind = ? ORDER BY ref", (kind,)
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        item = json.loads(r["payload"])
        # Surface the stable queue key the review PUT takes (/review/{kind}/<ref>). The payload holds only
        # display fields; the composite backbone-leaf ref (variety_norm|stone_type_norm|attribute|value_norm)
        # cannot be reconstructed client-side from those, so without this the UI has no valid ref to send and
        # correctly disables approve/reject. list_decided_leaves already exposes ref; pending now matches.
        item["ref"] = r["ref"]
        item["sources"] = json.loads(r["sources"]) if r["sources"] else []
        if kind == "variety":
            # a variety statement carries a mint/rename/reject AND may carry a bind
            _project_decision(item, dec)
        elif kind == "origin":
            # an origin card has NO mint/reject of its own -- its decision is the bind (+ the confirmed
            # country), so the SAME projection drives it with statements off; the country is origin-specific
            _project_decision(item, dec, with_statements=False)
            item["current_country"] = origin_actions.get(r["ref"])
            # a card settled purely by confirming the country (no bind) still reads decided, action "origin"
            if origin_actions.get(r["ref"]) is not None:
                item["decided"] = True
                item["current_action"] = item.get("current_action") or "origin"
        elif kind == "backbone_leaf":
            item["current_action"] = leaf_actions.get(r["ref"])
            item["decided"] = leaf_actions.get(r["ref"]) is not None
        out.append(item)
    return out


# -- acting on a pending backbone-leaf item (a queue consumer that records a leaf verdict) ------------------

def _decide_leaf_from_payload(payload: dict, action: str) -> None:
    leaves.set_backbone_leaf_decision(payload.get("variety", ""), payload.get("stone_type", ""),
                                      payload.get("attribute", ""), payload.get("add_value", ""), action)


def approve_leaf_pending(verdict: str | None = None) -> int:
    """Bulk-approve the still-UNDECIDED pending backbone-leaf suggestions, optionally only those with
    `verdict` (e.g. 'likely_real'). Never re-flips a row the operator already decided (approve or reject).
    Returns the count approved. The approvals apply to the tree on the next produce (the standard
    'applies next produce' contract), and the decided rows drop off the queue on that run."""
    n = 0
    for item in list_pending("backbone_leaf"):
        if item.get("current_action") is not None:      # already decided; do not re-flip
            continue
        if verdict and item.get("verdict") != verdict:
            continue
        _decide_leaf_from_payload(item, "approve")
        n += 1
    return n


def decide_leaf_pending(ref: str, action: str) -> bool:
    """Record one operator verdict (approve|reject) on the pending backbone-leaf suggestion identified by
    `ref`. Reads the queued payload so the client need not resend the display fields. Returns False if no
    such pending item (already decided or unknown ref)."""
    payload = pending_payload("backbone_leaf", ref)
    if payload is None:
        return False
    _decide_leaf_from_payload(payload, action)
    return True
