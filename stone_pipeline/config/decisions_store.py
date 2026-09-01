"""The durable owner of new-variant review decisions.

ONE store, ONE place for a storage bug to live. Everything the operator decides about an uncertain
variety (mint / reject / alias-to-existing) and every new-attribute id they paste lives in config.db
-- which is already snapshotted + restored, so these decisions survive an ECS task restart (unlike the
old ephemeral CSVs under /app). Two callers use this module and nothing reimplements storage:

  - the produce side (`stages/decisions.py`) READS decisions at the start of a run and REWRITES the
    pending queue at the end;
  - the config server (`config/server.py`) serves the pending queue to the :4200 admin and WRITES the
    operator's decisions back.

Design rules (kept deliberately strict so bugs stay local):
  - `action` unifies the two old CSVs: mint == the old `confirm=true`, reject == `confirm=false` AND the
    learned reject memory, alias == the new "this is really a spelling of X" action.
  - reads return an EMPTY result for a fresh store (a genuine empty set), and only RAISE on a real DB
    error -- there is no "file missing -> silently return {}" fallback path.
  - varieties are keyed by NORMALIZED name (pre- and post-sync stable; a pending variety has no Medusa
    id yet), attributes by (kind, normalized value).

No em dashes in code comments is design principle 2 elsewhere; this module keeps to it.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone

from stone_pipeline.config import store
from stone_pipeline.config.domain import active_pack
from stone_pipeline.matching import projections as proj

_ACTIONS = ("mint", "reject", "alias")
_PENDING_KINDS = ("variety", "attribute", "backbone_leaf")
# The vocabularies a backbone variety carries an allowed SET of; each maps to a leaf-decision `attribute`.
# Value additions to these are what the backbone-leaf loop grows (all already in Medusa). Declared by the
# active product-domain pack (all attributes except the identity disambiguator); stone = color/finish/quality.
_LEAF_ATTRIBUTES = tuple(active_pack().leaf_attributes)
_LEAF_ACTIONS = ("approve", "reject")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(s: str) -> str:
    return proj.norm(s or "")


class UnknownVariety(KeyError):
    """A decision was requested for a variety spelling that is not in the pending queue."""


class InvalidDecision(ValueError):
    """A decision payload the store refuses: bad action, or alias without a target."""


# -- variety decisions (produce READS these) -----------------------------------

def variety_actions() -> dict[str, dict]:
    """norm(variant) -> {'action': mint|reject|alias, 'alias_of', 'seed_color', 'seed_type',
    'seed_country'} for every decided variety. Empty for a fresh store."""
    with closing(store.open_store()) as conn:
        return {r["variant_norm"]: {"action": r["action"], "alias_of": r["alias_of"],
                                    "seed_color": r["seed_color"], "seed_type": r["seed_type"],
                                    "seed_country": r["seed_country"]}
                for r in conn.execute(
                    "SELECT variant_norm, action, alias_of, seed_color, seed_type, seed_country "
                    "FROM variety_decision")}


def variety_seed_colors() -> dict[str, str]:
    """norm(variant) -> the operator-chosen mint colour, for every MINT decision that set one. The next
    produce seeds the minted variety with this instead of the generic 'Natural' fallback."""
    return {n: d["seed_color"] for n, d in variety_actions().items()
            if d["action"] == "mint" and d["seed_color"]}


def variety_seed_types() -> dict[str, str]:
    """norm(variant) -> the operator-assigned stone type, for every MINT decision that set one. curate mints
    a type-less variety with this instead of holding it, and load_all folds it into ref.variety_seed_types so
    the matcher can bind a product to the operator-minted (name, type). No side effect (does NOT create the
    DB) on a fresh store: load_all reads this every build, so it must NOT materialise config.db -- mirroring
    variety_seed_countries. Without the guard, ref-build creates an empty config.db that then shadows the
    sources.yaml seed for load_source."""
    if not store.config_db_path().exists():
        return {}
    return {n: d["seed_type"] for n, d in variety_actions().items()
            if d["action"] == "mint" and d["seed_type"]}


def variety_seed_countries() -> dict[str, str]:
    """norm(variant) -> the operator-chosen ISO country of origin, for every MINT decision that set one.
    load_all overlays these onto origin_map as CONFIRMED per-variety rules (the effective origin map =
    curated CSV + minted decisions), so a minted variety carries the true origin the operator picked.
    Empty (no side effect -- does not create the DB) for a fresh store: load_all reads this every build,
    so it must NOT materialise config.db, mirroring backbone_leaf_overlay."""
    if not store.config_db_path().exists():
        return {}
    return {n: d["seed_country"] for n, d in variety_actions().items()
            if d["action"] == "mint" and d["seed_country"]}


def variety_seed_country_rules() -> dict[tuple[str, str], str]:
    """(norm(variant), norm(stone_type)) -> the operator-chosen ISO origin, for every MINT that set a
    country. TYPE-SCOPED so a homonym minted under different types carries different origins. A mint with no
    stone_type keys ('', ); apply_origin_overlay SKIPS it -- origin is (name, type) and a type-less origin can
    never emit. This is the shape apply_origin_overlay consumes (variety_seed_countries is the flat name->iso
    accessor). No side effect on a fresh store (load_all reads it every build)."""
    if not store.config_db_path().exists():
        return {}
    return {(n, _norm(d["seed_type"])): d["seed_country"]
            for n, d in variety_actions().items()
            if d["action"] == "mint" and d["seed_country"]}


def confirm_map() -> dict[str, str]:
    """norm(variant) -> 'yes'|'no' -- the mint/reject view the legacy confirm-file reader expects.
    alias decisions are NOT in this map; the alias router consumes them separately (`alias_map`)."""
    out: dict[str, str] = {}
    for name, dec in variety_actions().items():
        if dec["action"] == "mint":
            out[name] = "yes"
        elif dec["action"] == "reject":
            out[name] = "no"
    return out


def rejected_names() -> set[str]:
    """norm(variant) for every reject decision -- the learned 'never propose again' memory."""
    return {n for n, d in variety_actions().items() if d["action"] == "reject"}


def alias_map() -> dict[str, str]:
    """norm(spelling) -> alias_of (the target variety NAME) for every alias decision. The router adds
    the spelling to the target variety's alias set so the product resolves onto it."""
    return {n: d["alias_of"] for n, d in variety_actions().items()
            if d["action"] == "alias" and d["alias_of"]}


def alias_type_map() -> dict[str, str]:
    """norm(spelling) -> the alias TARGET's stone type, for alias decisions where the operator picked one.
    Disambiguates a multi-type target name (Black Sea = andesite + soapstone): the spelling aliases into
    the target variety of THIS type, not an arbitrary same-name one. Absent (no entry) when the operator
    did not choose a type; the router then falls back to the scraped row's own type, and holds if neither
    disambiguates."""
    return {n: d["seed_type"] for n, d in variety_actions().items()
            if d["action"] == "alias" and d.get("seed_type")}


def set_variety_decision(variant: str, action: str, alias_of: str | None = None,
                         seed_color: str | None = None, seed_type: str | None = None,
                         seed_country: str | None = None) -> None:
    """Upsert ONE operator decision. Raises InvalidDecision on a bad action or an alias with no target.
    Idempotent: re-deciding a variety overwrites the prior decision. For a MINT, `seed_color`, `seed_type`
    and `seed_country` are the colour / stone type / ISO origin to mint the variety with. For an ALIAS,
    `seed_type` is the TARGET variety's stone type (which of a multi-type name to alias into); seed_color /
    seed_country are ignored. reject ignores all three. The caller validates seed_country is a real ISO code."""
    action = (action or "").strip().lower()
    if action not in _ACTIONS:
        raise InvalidDecision(f"action must be one of {_ACTIONS}, got {action!r}")
    alias_of = (alias_of or "").strip() or None
    if action == "alias" and not alias_of:
        raise InvalidDecision("alias decision requires alias_of (an existing variety name)")
    if action != "alias":
        alias_of = None                      # only alias carries a target; keep the row unambiguous
    seed_color = (seed_color or "").strip() or None
    seed_type = (seed_type or "").strip() or None
    seed_country = (seed_country or "").strip().upper() or None
    # mint carries colour + type + country to create the variety with. alias ALSO carries seed_type, but
    # meaning the TARGET's stone type: a target NAME can exist under several types (Black Sea = andesite +
    # soapstone), so the operator picks WHICH one to alias into, else the alias cannot resolve. reject
    # carries nothing.
    if action == "alias":
        seed_color = seed_country = None
    elif action != "mint":
        seed_color = seed_type = seed_country = None
    norm = _norm(variant)
    if not norm:
        raise InvalidDecision("variant name is empty")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO variety_decision (variant_norm, variant_display, action, alias_of, seed_color, "
            "seed_type, seed_country, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(variant_norm) DO UPDATE SET "
            "variant_display = excluded.variant_display, action = excluded.action, "
            "alias_of = excluded.alias_of, seed_color = excluded.seed_color, "
            "seed_type = excluded.seed_type, seed_country = excluded.seed_country, "
            "decided_at = excluded.decided_at",
            (norm, variant.strip(), action, alias_of, seed_color, seed_type, seed_country, _now()))
        conn.commit()


def learn_rejects(names: set[str]) -> None:
    """Persist runtime-learned rejects (curate marks a code-shaped name it was told 'no' on). INSERT OR
    IGNORE so it never overwrites an explicit mint/alias decision the operator made."""
    if not names:
        return
    now = _now()
    with closing(store.open_store()) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO variety_decision "
            "(variant_norm, variant_display, action, alias_of, decided_at) VALUES (?, ?, 'reject', NULL, ?)",
            [(_norm(n), n, now) for n in names if _norm(n)])
        conn.commit()


# -- attribute decisions (produce READS these) ---------------------------------

def attribute_ids() -> dict[tuple[str, str], tuple[str, str]]:
    """(kind, norm(value)) -> (ORIGINAL value, medusa_id) for the ids the operator pasted. The original
    value (operator casing) becomes the canonical attribute name, not its normalization."""
    with closing(store.open_store()) as conn:
        return {(r["kind"], r["value_norm"]): (r["value_display"], r["medusa_id"])
                for r in conn.execute(
                    "SELECT kind, value_norm, value_display, medusa_id FROM attribute_decision")}


def set_attribute_id(kind: str, value: str, medusa_id: str) -> None:
    kind = (kind or "").strip().lower()
    value = (value or "").strip()
    medusa_id = (medusa_id or "").strip()
    if not (kind and value and medusa_id):
        raise InvalidDecision("attribute decision needs kind, value, and medusa_id")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO attribute_decision (kind, value_norm, value_display, medusa_id, decided_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(kind, value_norm) DO UPDATE SET "
            "value_display = excluded.value_display, medusa_id = excluded.medusa_id, "
            "decided_at = excluded.decided_at",
            (kind, _norm(value), value, medusa_id, _now()))
        conn.commit()


def clear_attribute_ids() -> int:
    """Drop every operator-pasted attribute id. Paired with a GLOBAL ledger reset: after Medusa is wiped
    those ids point at entities that no longer exist, so clearing them makes the next catalog re-derive
    from the fresh attributes.csv export and re-prompt for any still needed, instead of adopting a dead id.
    Returns the number of rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM attribute_decision").rowcount
        conn.commit()
    return n


def clear_variety_decisions() -> int:
    """Drop EVERY operator variety decision (mint/reject/alias + seed colour/type/country). PRISTINE
    (factory) reset ONLY: a normal soft/hard reset KEEPS these on purpose (curation you want to survive a
    sync reset), but a cold start back to the committed seed must forget them, else a previously minted or
    aliased variety silently re-applies on the next produce and the 'clean' catalog is not seed-only.
    Returns the number of rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM variety_decision").rowcount
        conn.commit()
    return n


def clear_leaf_decisions() -> int:
    """Drop EVERY backbone leaf-growth decision (approve/reject). PRISTINE reset ONLY, same rationale as
    clear_variety_decisions: an approved leaf grows the tree via the load-time overlay (apply_leaf_overlay),
    so it must be forgotten for a cold start to reproduce the committed backbone. Returns the rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM backbone_leaf_decision").rowcount
        conn.commit()
    return n


def protected_keys() -> set[str]:
    """Variation Keys the operator marked 'not a duplicate'. The reconcile dedup must never drop these,
    so a false-positive collapse (two genuinely distinct varieties) cannot re-tombstone them. Empty set
    for a fresh store."""
    with closing(store.open_store()) as conn:
        return {r["key"] for r in conn.execute("SELECT key FROM protected_variation")}


def add_protected(key: str) -> None:
    """Record a 'not a duplicate' verdict for a variation Key (idempotent: a repeat is a no-op)."""
    with closing(store.open_store()) as conn:
        conn.execute("INSERT OR IGNORE INTO protected_variation (key, decided_at) VALUES (?, ?)",
                     (key, _now()))
        conn.commit()


def clear_protected() -> int:
    """Drop EVERY 'not a duplicate' verdict. PRISTINE reset ONLY, same rationale as clear_variety_decisions:
    a cold start back to the committed seed must forget operator dedup overrides. Returns rows dropped."""
    with closing(store.open_store()) as conn:
        n = conn.execute("DELETE FROM protected_variation").rowcount
        conn.commit()
    return n


# -- backbone leaf-growth decisions (produce READS the overlay + decided set) --

def backbone_leaf_overlay() -> dict[tuple[str, str], dict[str, list[str]]]:
    """(variety_norm, stone_type_norm) -> {attribute: [display values]} for APPROVED leaf additions only.
    This is the load-time overlay the backbone is grown with (loaders.Backbone.apply_leaf_overlay); the
    committed seed JSON is never touched. Empty for a fresh store (no side effect: does not create the DB),
    and rejected rows are absent (a reject is just 'do not add')."""
    if not store.config_db_path().exists():
        return {}
    out: dict[tuple[str, str], dict[str, list[str]]] = {}
    with closing(store.open_store()) as conn:
        for r in conn.execute("SELECT variety_norm, stone_type_norm, attribute, value_display "
                              "FROM backbone_leaf_decision WHERE action = 'approve'"):
            key = (r["variety_norm"], r["stone_type_norm"])
            out.setdefault(key, {}).setdefault(r["attribute"], []).append(r["value_display"])
    return out


def leaf_decided() -> set[tuple[str, str, str, str]]:
    """(variety_norm, stone_type_norm, attribute, value_norm) for every DECIDED leaf suggestion (approve
    OR reject), so the produce drops them from the pending queue -- a decided item stops reappearing."""
    if not store.config_db_path().exists():
        return set()
    with closing(store.open_store()) as conn:
        return {(r["variety_norm"], r["stone_type_norm"], r["attribute"], r["value_norm"])
                for r in conn.execute("SELECT variety_norm, stone_type_norm, attribute, value_norm "
                                      "FROM backbone_leaf_decision")}


def _leaf_actions_by_ref() -> dict[str, str]:
    """ref ('variety_norm|stone_type_norm|attribute|value_norm') -> action, for the UI to reflect a
    between-runs decision on a still-pending row (mirrors variety `current_action`)."""
    with closing(store.open_store()) as conn:
        return {"|".join((r["variety_norm"], r["stone_type_norm"], r["attribute"], r["value_norm"])): r["action"]
                for r in conn.execute("SELECT variety_norm, stone_type_norm, attribute, value_norm, action "
                                      "FROM backbone_leaf_decision")}


def set_backbone_leaf_decision(variety: str, stone_type: str, attribute: str, value: str,
                               action: str) -> None:
    """Record the operator's verdict on adding `value` (a colour/finish/quality Medusa already has) onto
    `variety` (disambiguated by stone_type). approve -> the value joins the variety's allowed set as a
    load-time overlay; reject -> never propose it again. The committed backbone seed is never touched."""
    attribute = (attribute or "").strip().lower()
    action = (action or "").strip().lower()
    if attribute not in _LEAF_ATTRIBUTES:
        raise InvalidDecision(f"leaf attribute must be one of {_LEAF_ATTRIBUTES}, got {attribute!r}")
    if action not in _LEAF_ACTIONS:
        raise InvalidDecision(f"leaf action must be approve|reject, got {action!r}")
    if not (variety.strip() and value.strip()):
        raise InvalidDecision("leaf decision needs a variety and a value")
    with closing(store.open_store()) as conn:
        conn.execute(
            "INSERT INTO backbone_leaf_decision (variety_norm, stone_type_norm, attribute, value_norm, "
            "value_display, action, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(variety_norm, stone_type_norm, attribute, value_norm) DO UPDATE SET "
            "value_display = excluded.value_display, action = excluded.action, decided_at = excluded.decided_at",
            (_norm(variety), _norm(stone_type), attribute, _norm(value), value.strip(), action, _now()))
        conn.commit()


def _decide_leaf_from_payload(payload: dict, action: str) -> None:
    set_backbone_leaf_decision(payload.get("variety", ""), payload.get("stone_type", ""),
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


def list_decided_leaves() -> list[dict]:
    """Every recorded backbone-leaf verdict (approve OR reject), newest first, each carrying the `ref` the
    review PUT takes -- so an operator can find and revise a past decision (e.g. undo a spurious approval a
    prior run already grew into the overlay). Empty for a fresh store, with no side effect (does not create
    the DB)."""
    if not store.config_db_path().exists():
        return []
    with closing(store.open_store()) as conn:
        rows = conn.execute(
            "SELECT variety_norm, stone_type_norm, attribute, value_norm, value_display, action, decided_at "
            "FROM backbone_leaf_decision ORDER BY decided_at DESC, value_display").fetchall()
    return [{"variety": r["variety_norm"], "stone_type": r["stone_type_norm"], "attribute": r["attribute"],
             "add_value": r["value_display"], "action": r["action"], "decided_at": r["decided_at"],
             "ref": "|".join((r["variety_norm"], r["stone_type_norm"], r["attribute"], r["value_norm"]))}
            for r in rows]


def revise_leaf_decision(ref: str, action: str) -> bool:
    """Change or remove an ALREADY-decided backbone leaf, by `ref`. decide_leaf_pending only reaches a leaf
    still in the pending queue; once decided, a leaf drops off that queue, so revising it works off the
    recorded decision instead. `action`:
      approve|reject -> flip the verdict (un-approving a grown leaf: approve -> reject drops it from the
                        overlay AND keeps it decided, so it is never re-suggested);
      clear          -> delete the verdict, returning the leaf to pristine (it stops growing the tree; the
                        suggestion re-surfaces on a later produce only if a product still implies it).
    Returns False if `ref` names no decided leaf. The change applies on the next produce, like every other
    decision (the overlay is read at load_backbone)."""
    action = (action or "").strip().lower()
    if action not in ("approve", "reject", "clear"):
        raise InvalidDecision(f"leaf revision must be approve|reject|clear, got {action!r}")
    parts = ref.split("|")
    if len(parts) != 4 or not store.config_db_path().exists():
        return False
    where = "WHERE variety_norm = ? AND stone_type_norm = ? AND attribute = ? AND value_norm = ?"
    with closing(store.open_store()) as conn:
        if action == "clear":
            cur = conn.execute(f"DELETE FROM backbone_leaf_decision {where}", tuple(parts))
        else:
            cur = conn.execute(f"UPDATE backbone_leaf_decision SET action = ?, decided_at = ? {where}",
                               (action, _now(), *parts))
        conn.commit()
    return cur.rowcount > 0


# -- the pending review queue (produce WRITES it, the API READS it) -------------

def replace_pending(kind: str, rows: list[dict]) -> None:
    """Wholly replace the pending queue for `kind` ('variety'|'attribute'|'backbone_leaf') with this
    produce's undecided items. A decided item simply is not in `rows`, so it stops appearing. Each row must
    carry `ref` (the stable key) and `payload` (the JSON-able dict the UI renders); `sources` (list) is
    optional provenance."""
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
    with closing(store.open_store()) as conn:
        r = conn.execute("SELECT payload FROM review_pending WHERE kind = ? AND ref = ?",
                         (kind, ref)).fetchone()
    return json.loads(r["payload"]) if r else None


def list_pending(kind: str) -> list[dict]:
    """The pending items for `kind`, each = its payload plus `sources` and the `current_action` already
    recorded for it (so the UI can show a decision made between runs, applied on the next produce)."""
    actions = variety_actions() if kind == "variety" else {}
    leaf_actions = _leaf_actions_by_ref() if kind == "backbone_leaf" else {}
    with closing(store.open_store()) as conn:
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
            item["current_action"] = actions.get(r["ref"], {}).get("action")
            item["current_alias_of"] = actions.get(r["ref"], {}).get("alias_of")
            item["current_seed_color"] = actions.get(r["ref"], {}).get("seed_color")
            item["current_seed_type"] = actions.get(r["ref"], {}).get("seed_type")
            item["current_seed_country"] = actions.get(r["ref"], {}).get("seed_country")
        elif kind == "backbone_leaf":
            item["current_action"] = leaf_actions.get(r["ref"])
        out.append(item)
    return out
