"""The backbone leaf-growth loop: surface -> review/approve -> load-time overlay -> release.

An old backbone allows only what it was seeded with (e.g. quality A). When a scrape carries a value
Medusa already has but the variety does not allow yet (quality B/C/D, a colour, a finish), the pipeline
SURFACES it for review; an operator APPROVES it; the approval grows the backbone as a load-time OVERLAY
so the previously-blocked product passes is_valid_leaf on the next produce. The committed seed JSON is
NEVER mutated -- dropping the overlay restores the pristine tree. The cheap release path is the new
`republish` stage (re-run pipeline + catalog from the last scrape, no supplier re-fetch).

These are pure tests (no sockets, no live scrape): the autouse conftest fixture isolates config.db.
"""

from __future__ import annotations

import json
import sqlite3

from stone_pipeline import build, produce
from stone_pipeline.config import decisions_store as ds
from stone_pipeline.config import runner, server
from stone_pipeline.config import store
from stone_pipeline.reference.loaders import Backbone, BackboneVariety, _norm, load_backbone
from stone_pipeline.stages import decisions


def _variety(quals=("A",), finishes=("Polished",), colors=("Black",), stone_type="Granite"):
    return BackboneVariety(variant="Tiger Black", category="Slabs", stone_type=stone_type,
                           colors=list(colors), finishes=list(finishes), qualities=list(quals), aliases=[])


def _backbone(variety) -> Backbone:
    bb = Backbone()
    bb.by_norm_name[_norm(variety.variant)] = [variety]
    return bb


def _suggestion(attribute="quality", value="B", stone_type="Granite", verdict="likely_real"):
    return {"variety": "Tiger Black", "stone_type": stone_type, "attribute": attribute,
            "add_value": value, "currently_allowed": "A", "match_method": "exact",
            "match_confidence": "0.99", "verdict": verdict, "example_url": "http://x"}


# -- the overlay (pure, seed never mutated) ------------------------------------

def test_overlay_grows_the_allowed_set_and_releases_the_leaf():
    v = _variety(quals=("A",))
    bb = _backbone(v)
    assert bb.is_valid_leaf(v, "", "", "B") is False          # blocked before growth
    added = bb.apply_leaf_overlay({("tiger black", "granite"): {"quality": ["B"]}})
    assert added == 1 and v.qualities == ["A", "B"]
    assert bb.is_valid_leaf(v, "", "", "B") is True           # released after growth


def test_overlay_is_idempotent():
    v = _variety(quals=("A", "B"))
    bb = _backbone(v)
    assert bb.apply_leaf_overlay({("tiger black", "granite"): {"quality": ["B"]}}) == 0
    assert v.qualities == ["A", "B"]                          # no duplicate


def test_overlay_disambiguates_by_stone_type():
    """A granite approval must not grow the same-named quartzite variety (no fuzzy fallback)."""
    quartzite = _variety(stone_type="Quartzite")
    bb = _backbone(quartzite)
    assert bb.apply_leaf_overlay({("tiger black", "granite"): {"quality": ["B"]}}) == 0
    assert "B" not in quartzite.qualities


def test_overlay_routes_each_attribute_to_its_set():
    v = _variety(quals=("A",), finishes=("Polished",), colors=("Black",))
    _backbone(v).apply_leaf_overlay({("tiger black", "granite"):
                                     {"quality": ["B"], "finish": ["Honed"], "color": ["White"]}})
    assert v.qualities == ["A", "B"] and v.finishes == ["Polished", "Honed"] and v.colors == ["Black", "White"]


def test_load_backbone_never_mutates_the_seed_file(tmp_path):
    """The clean-base guarantee: growth is an in-memory overlay; the committed seed JSON is untouched."""
    seed = tmp_path / "backbone.json"
    payload = {"posts": [{"variant": "Tiger Black", "category": "Slabs", "stone_type": "Granite",
                          "color": ["Black"], "finishes": ["Polished"], "qualities": ["A"]}]}
    seed.write_text(json.dumps(payload), encoding="utf-8")
    before = seed.read_bytes()
    bb = load_backbone(seed)
    bb.apply_leaf_overlay({("tiger black", "granite"): {"quality": ["B", "C", "D"]}})
    assert bb.by_norm_name["tiger black"][0].qualities == ["A", "B", "C", "D"]   # grown in memory
    assert seed.read_bytes() == before                                          # file byte-identical


# -- the durable decision store ------------------------------------------------

def test_approved_becomes_overlay_rejected_does_not():
    ds.set_backbone_leaf_decision("Tiger Black", "Granite", "quality", "B", "approve")
    ds.set_backbone_leaf_decision("Tiger Black", "Granite", "quality", "C", "reject")
    assert ds.backbone_leaf_overlay() == {("tiger black", "granite"): {"quality": ["B"]}}
    # both are 'decided', so both drop off the pending queue
    assert ds.leaf_decided() == {("tiger black", "granite", "quality", "b"),
                                 ("tiger black", "granite", "quality", "c")}


def test_overlay_is_empty_and_side_effect_free_without_a_store(monkeypatch, tmp_path):
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "absent.db"))
    assert ds.backbone_leaf_overlay() == {} and ds.leaf_decided() == set()
    assert not (tmp_path / "absent.db").exists()               # reading did not create the DB


def test_set_decision_validates_attribute_and_action():
    import pytest
    with pytest.raises(ds.InvalidDecision):
        ds.set_backbone_leaf_decision("V", "T", "weight", "9", "approve")   # not a leaf attribute
    with pytest.raises(ds.InvalidDecision):
        ds.set_backbone_leaf_decision("V", "T", "quality", "B", "mint")     # not a leaf action


# -- the pending queue (surface -> decided drops off) --------------------------

def test_surface_then_decided_drops_off_the_queue():
    assert decisions.write_backbone_leaf_pending([_suggestion()]) == 1
    assert [i["add_value"] for i in ds.list_pending("backbone_leaf")] == ["B"]
    ds.approve_leaf_pending()
    assert decisions.write_backbone_leaf_pending([_suggestion()]) == 0   # decided -> not re-surfaced


def test_incomplete_suggestions_are_skipped():
    rows = [_suggestion(), {"variety": "", "attribute": "quality", "add_value": "B"}]
    assert decisions.write_backbone_leaf_pending(rows) == 1              # the blank-variety row is dropped


# -- the review API (:4200) ----------------------------------------------------

def test_get_lists_pending_with_current_action():
    decisions.write_backbone_leaf_pending([_suggestion(verdict="verify_match")])
    code, body = server.dispatch("GET", ["review", "backbone"], None)
    assert code == 200 and body["backbone"][0]["add_value"] == "B"
    assert body["backbone"][0]["current_action"] is None                # undecided


def test_pending_item_carries_the_ref_the_put_accepts():
    # The review UI enables approve/reject off b.ref; the composite leaf ref cannot be rebuilt client-side
    # from the display fields, so the GET must hand it over -- and it must be exactly the ref the PUT resolves.
    decisions.write_backbone_leaf_pending([_suggestion()])
    item = server.dispatch("GET", ["review", "backbone"], None)[1]["backbone"][0]
    assert item["ref"] == "|".join(("tiger black", "granite", "quality", "b"))
    # round-trip: the ref straight off the list decides the leaf (the frontend sends it back verbatim)
    code, _ = server.dispatch("PUT", ["review", "backbone", item["ref"]], {"action": "approve"})
    assert code == 200 and ds.backbone_leaf_overlay() == {("tiger black", "granite"): {"quality": ["B"]}}


def test_put_single_verdict_then_reflected():
    decisions.write_backbone_leaf_pending([_suggestion()])
    ref = "|".join(("tiger black", "granite", "quality", "b"))
    code, body = server.dispatch("PUT", ["review", "backbone", ref], {"action": "approve"})
    assert code == 200 and body["action"] == "approve"
    assert ds.backbone_leaf_overlay() == {("tiger black", "granite"): {"quality": ["B"]}}
    # the still-queued row now reports the between-runs decision
    assert server.dispatch("GET", ["review", "backbone"], None)[1]["backbone"][0]["current_action"] == "approve"


def test_put_bad_action_is_400_and_unknown_ref_is_404():
    decisions.write_backbone_leaf_pending([_suggestion()])
    ref = "|".join(("tiger black", "granite", "quality", "b"))
    assert server.dispatch("PUT", ["review", "backbone", ref], {"action": "nope"})[0] == 400
    assert server.dispatch("PUT", ["review", "backbone", ref], "not-a-dict")[0] == 400
    assert server.dispatch("PUT", ["review", "backbone", "no|such|quality|x"], {"action": "approve"})[0] == 404


def test_bulk_approve_all_and_by_verdict():
    decisions.write_backbone_leaf_pending([
        _suggestion(value="B", verdict="likely_real"),
        _suggestion(attribute="finish", value="Honed", verdict="verify_match")])
    # only the confident one, first
    assert server.dispatch("POST", ["review", "backbone", "approve_all"], {"verdict": "likely_real"})[1]["approved"] == 1
    assert ds.backbone_leaf_overlay() == {("tiger black", "granite"): {"quality": ["B"]}}
    # then the rest
    assert server.dispatch("POST", ["review", "backbone", "approve_all"], {})[1]["approved"] == 1
    assert set(ds.backbone_leaf_overlay()[("tiger black", "granite")]) == {"quality", "finish"}


# -- un-approving an already-decided leaf (revise a past verdict) ----------------

def test_revise_unapproves_a_decided_leaf_the_pending_path_cannot_reach():
    # a leaf a prior run APPROVED and grew into the overlay, later found spurious. Decided rows drop off the
    # pending queue, so decide_leaf_pending can no longer reach it -- revise_leaf_decision is the way back.
    ds.set_backbone_leaf_decision("Tiger Black", "Granite", "quality", "B", "approve")
    ref = "|".join(("tiger black", "granite", "quality", "b"))
    assert ds.backbone_leaf_overlay() == {("tiger black", "granite"): {"quality": ["B"]}}
    assert ds.decide_leaf_pending(ref, "reject") is False        # not pending: the queue can't touch it

    assert ds.revise_leaf_decision(ref, "reject") is True         # flip: drops from the overlay...
    assert ds.backbone_leaf_overlay() == {}
    assert ("tiger black", "granite", "quality", "b") in ds.leaf_decided()   # ...but stays remembered

    assert ds.revise_leaf_decision(ref, "clear") is True          # clear: back to pristine
    assert ds.leaf_decided() == set()
    assert ds.revise_leaf_decision(ref, "reject") is False        # nothing left to revise -> no-op, not error


def test_revise_rejects_bad_action_and_malformed_ref():
    import pytest
    ds.set_backbone_leaf_decision("Tiger Black", "Granite", "quality", "B", "approve")
    with pytest.raises(ds.InvalidDecision):
        ds.revise_leaf_decision("tiger black|granite|quality|b", "banana")
    assert ds.revise_leaf_decision("too|few|parts", "clear") is False


def test_list_decided_leaves_exposes_refs():
    ds.set_backbone_leaf_decision("Tiger Black", "Granite", "quality", "B", "approve")
    ds.set_backbone_leaf_decision("Tiger Black", "Granite", "finish", "Honed", "reject")
    decided = ds.list_decided_leaves()
    assert {d["add_value"]: d["action"] for d in decided} == {"B": "approve", "Honed": "reject"}
    approved = next(d for d in decided if d["action"] == "approve")
    assert approved["ref"] == "|".join(("tiger black", "granite", "quality", "b"))
    assert ds.revise_leaf_decision(approved["ref"], "clear") is True   # the ref round-trips through revise


def test_put_unapproves_a_decided_leaf_end_to_end():
    # the operator flow: a spurious approval exists; GET decided finds it by ref; PUT reject un-approves it.
    ds.set_backbone_leaf_decision("Tiger Black", "Granite", "quality", "B", "approve")
    code, body = server.dispatch("GET", ["review", "backbone", "decided"], None)
    assert code == 200 and body["decided"][0]["action"] == "approve"
    ref = body["decided"][0]["ref"]
    code, body = server.dispatch("PUT", ["review", "backbone", ref], {"action": "reject"})
    assert code == 200 and body["action"] == "reject"
    assert ds.backbone_leaf_overlay() == {}                       # no longer grows the tree
    # a clear through the API on an unknown/already-gone ref is a clean 404, not a 500
    assert server.dispatch("PUT", ["review", "backbone", "no|such|quality|x"], {"action": "clear"})[0] == 404


# -- the republish stage (release without a supplier re-fetch) ------------------

def test_stages_lists_are_in_sync():
    """runner validates the trigger without importing the heavy pipeline, so it duplicates build.STAGES.
    Lock the two together so they can never drift."""
    assert runner.STAGES == build.STAGES
    assert "republish" in build.STAGES


def test_build_republish_runs_pipeline_and_catalog(monkeypatch):
    calls = []
    monkeypatch.setattr(build, "_scrape", lambda s: calls.append(("scrape", s)) or 0)
    monkeypatch.setattr(build, "_catalog", lambda: calls.append(("catalog", None)) or 0)
    assert build.main(["--stage", "republish"]) == 0
    assert calls == [("scrape", None), ("catalog", None)]     # full pipeline + catalog, like `all`


def test_produce_republish_skips_the_live_scrape(monkeypatch):
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append(("fetch", None)))
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append(("scrape", s)) or 0)
    monkeypatch.setattr(produce.build, "main", lambda argv: calls.append(("build", argv)) or 0)
    assert produce.main(["--stage", "republish"]) == 0
    assert calls == [("fetch", None), ("build", ["--stage", "republish"])]   # NO scrape


def test_runner_accepts_republish_stage():
    assert "republish" in runner.STAGES


# -- the CHECK migration (legacy DBs) ------------------------------------------

def test_legacy_review_pending_check_is_migrated(monkeypatch, tmp_path):
    """A config.db created before the queue was generic pins kind in a CHECK; opening it must drop the
    CHECK (carrying rows across) so backbone_leaf items insert."""
    db = tmp_path / "legacy.db"
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(db))
    conn = sqlite3.connect(str(db))
    conn.executescript(store._SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("CREATE TABLE review_pending (kind TEXT NOT NULL CHECK (kind IN ('variety','attribute')), "
                 "ref TEXT NOT NULL, payload TEXT NOT NULL, sources TEXT, updated_at TEXT NOT NULL, "
                 "PRIMARY KEY (kind, ref))")
    conn.execute("INSERT INTO review_pending VALUES ('variety', 'x', '{}', NULL, 't')")
    conn.commit()
    conn.close()

    decisions.write_backbone_leaf_pending([_suggestion()])    # would fail the old CHECK
    assert len(ds.list_pending("backbone_leaf")) == 1
    with store.open_store() as c:
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'review_pending'").fetchone()[0]
    assert "CHECK" not in sql
