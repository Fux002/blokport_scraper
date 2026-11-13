"""The 're-produce to apply' signal: operator decisions recorded after the last catalog-applying produce
are not yet reflected in the emitted catalog. store.decisions_pending_apply computes it with no product
scan (two aggregate reads over the decision tables + the bounded run_log).

Pure tests: the autouse conftest fixture isolates config.db.
"""

from __future__ import annotations

from stone_pipeline.config import decisions_store as ds
from stone_pipeline.config import store

_PAST = "2000-01-01T00:00:00+00:00"
_FUTURE = "2999-01-01T00:00:00+00:00"


def _run(run_id: str, finished_at: str, status: str = "succeeded", stage: str = "all",
         started_at: str | None = None) -> None:
    store.record_run_log({"run_id": run_id, "started_at": started_at or finished_at, "finished_at": finished_at,
                          "status": status, "stage": stage})


def _decide(name: str) -> None:
    ds.decide("varsha", name, name, "Granite", color="White",
              exists_as=lambda n, t: False, alias_target=lambda n, t: None)


def test_no_produce_yet_counts_every_decision():
    _decide("Alpine Luxe")
    got = store.decisions_pending_apply()
    assert got["last_produce_at"] is None
    assert got["changed"] >= 1                     # nothing has applied them yet -> all pending
    assert got["latest_decision_at"] is not None


def test_one_mint_counts_as_one_decision_not_its_vendor_plus_global_rows():
    _decide("Alpine Luxe")                          # writes a vendor statement AND a global (source='') one
    # counted by DISTINCT spelling_norm, so the pair is one decision, not two
    assert store.decisions_pending_apply()["changed"] == 1


def test_decision_after_the_last_produce_is_pending():
    _run("zucchi_1", _PAST)                         # last applied produce is in the past
    _decide("Alpine Luxe")                          # ...decided now, i.e. AFTER it
    got = store.decisions_pending_apply()
    assert got["last_produce_at"] == _PAST
    assert got["changed"] >= 1


def test_decision_before_the_last_produce_is_not_pending():
    _decide("Alpine Luxe")                          # decided now
    _run("zucchi_1", _FUTURE)                       # ...a produce finished "after" every decision
    got = store.decisions_pending_apply()
    assert got["last_produce_at"] == _FUTURE
    assert got["changed"] == 0                      # the catalog already reflects them


def test_a_scrape_only_run_does_not_count_as_applied():
    _decide("Alpine Luxe")
    _run("zucchi_scrape", _FUTURE, stage="scrape")  # a scrape applies no decisions...
    got = store.decisions_pending_apply()
    assert got["last_produce_at"] is None           # ...so it is not the applied baseline
    assert got["changed"] >= 1                      # the decision is still pending a catalog produce


def test_a_failed_produce_does_not_count_as_applied():
    _decide("Alpine Luxe")
    _run("zucchi_fail", _FUTURE, status="failed")   # a failed produce emitted nothing
    got = store.decisions_pending_apply()
    assert got["last_produce_at"] is None
    assert got["changed"] >= 1


def test_the_newest_applied_produce_wins_over_an_older_one():
    _decide("Alpine Luxe")                          # decided "between" the two produces
    _run("zucchi_old", _PAST)                       # an older applied produce
    _run("zucchi_new", _FUTURE)                     # a newer one that DID reflect the decision
    got = store.decisions_pending_apply()
    assert got["last_produce_at"] == _FUTURE        # compares against the most recent applied run
    assert got["changed"] == 0


def test_a_statement_made_while_the_applying_produce_ran_is_still_pending():
    # a produce reads the decisions ONCE, at curate start: a statement made while it ran reached no product,
    # though it predates the run's finish
    _decide("Alpine Luxe")                          # decided "during" the run below
    _run("zucchi_long", _FUTURE, started_at=_PAST)  # started before the statement, finished after it
    assert store.decisions_pending_apply()["changed"] == 1


def test_membership_the_produce_writes_is_never_an_operator_decision_pending(seeded_config_store):
    # a minted variety's colour/finish/quality, recorded by the produce itself (record_minted_membership), is written
    # during the run, after its start: it is the produce's own output, not a decision waiting for one
    assert ds.record_minted_membership([("Alpine Luxe", "Granite", "color", "White")]) == 1
    got = store.decisions_pending_apply()
    assert got["changed"] == 0 and got["latest_decision_at"] is None
    _run("zucchi_long", _FUTURE, started_at=_PAST)
    assert ds.record_minted_membership([("Alpine Luxe", "Granite", "finish", "Polished")]) == 1
    assert store.decisions_pending_apply()["changed"] == 0


def test_an_operator_leaf_verdict_counts_and_takes_over_a_produce_written_row(seeded_config_store):
    ds.record_minted_membership([("Alpine Luxe", "Granite", "color", "White")])
    ds.set_backbone_leaf_decision("Alpine Luxe", "Granite", "color", "White", "reject")   # the operator revises it
    ds.set_backbone_leaf_decision("Alpine Luxe", "Granite", "quality", "B", "approve")
    assert store.decisions_pending_apply()["changed"] == 2


def test_the_produce_never_takes_over_an_operator_verdict_on_the_same_leaf():
    ds.set_backbone_leaf_decision("Alpine Luxe", "Granite", "color", "White", "reject")    # the operator's
    assert ds.record_minted_membership([("Alpine Luxe", "Granite", "color", "White")]) == 0
    assert [(d["action"]) for d in ds.list_decided_leaves()] == ["reject"]
    assert store.decisions_pending_apply()["changed"] == 1                               # still the operator's
