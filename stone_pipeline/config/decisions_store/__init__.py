"""The durable owner of the operator's review decisions.

ONE store, in config.db (snapshotted + restored, so decisions survive an ECS task restart), split by concern
into one module per table so a storage bug stays local:

  - statements  -- THE decision model: the `statement` table (for vendor S the spelling X is variety N, or a
                   reject), its reader/builder `load_decisions` (into ONE config.decisions_model.Decisions the
                   stages consult), and the writers `decide` / `reject` / `clear`. Mint-vs-bind is derived at
                   read time, never stored.
  - origins     -- per-variety origin edits (the "edit origins" admin action; a country LIST per variety).
  - attributes  -- operator-pasted Medusa ids for new colour/finish/type/quality values.
  - protected   -- 'not a duplicate' verdicts the reconcile dedup must never drop.
  - leaves      -- backbone leaf-growth verdicts (approve/reject a value onto a variety's allowed set).
  - queue       -- the pending review queue produce WRITES and the API READS, plus the card projection.

This module is the FACADE: callers use `decisions_store.<name>` and nothing imports the submodules directly,
so a test that patches `decisions_store.load_decisions` is honored by every internal caller (which resolves
the name through this package). No em dashes in code comments (design principle 2)."""

from __future__ import annotations

from ._common import InvalidDecision
from .attributes import attribute_ids, clear_attribute_ids, set_attribute_id
from .leaves import (_LEAF_ATTRIBUTES, backbone_leaf_overlay, clear_leaf_decisions, drop_backbone_membership,
                     leaf_decided, list_decided_leaves, record_minted_membership, revise_leaf_decision,
                     set_backbone_leaf_decision)
from .origins import (clear_variety_origins, delete_variety_origin, get_variety_origin, list_variety_origins,
                      set_variety_origin, variety_origins)
from .protected import add_protected, clear_protected, protected_keys
from .queue import (approve_leaf_pending, clear_review_pending, decide_leaf_pending, list_pending,
                    pending_payload, replace_pending)
from .statements import (_statement_rows, _upsert_statement, clear, clear_all_statements, clear_for_variety,
                         decide, load_decisions, mint_seed_colors, reject, scope_key)

__all__ = [
    "InvalidDecision",
    # statements
    "scope_key", "load_decisions", "decide", "reject", "clear", "clear_for_variety", "clear_all_statements",
    "_statement_rows", "_upsert_statement", "mint_seed_colors",
    # per-variety origins
    "set_variety_origin", "delete_variety_origin", "variety_origins", "list_variety_origins",
    "get_variety_origin", "clear_variety_origins",
    # attribute ids
    "attribute_ids", "set_attribute_id", "clear_attribute_ids",
    # protected variations
    "protected_keys", "add_protected", "clear_protected",
    # backbone leaf-growth
    "backbone_leaf_overlay", "leaf_decided", "set_backbone_leaf_decision", "approve_leaf_pending",
    "decide_leaf_pending", "list_decided_leaves", "revise_leaf_decision", "clear_leaf_decisions",
    "record_minted_membership", "drop_backbone_membership", "_LEAF_ATTRIBUTES",
    # pending review queue
    "replace_pending", "clear_review_pending", "pending_payload", "list_pending",
]
