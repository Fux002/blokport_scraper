"""No produce-path write creates config.db.

On the sync service config.db is restored from S3 at boot, BEFORE seed_from_yaml, and the restore runs only when the
local file is absent: a store a produce write had created (empty of sources) would make the restore skip. So the config
server's boot is the only creator, and every produce-side writer opens the store through the ONE refusing opener,
store.open_existing_store: the review queue (decisions_store.replace_pending), the minted membership
(decisions_store.record_minted_membership) and the learned aliases. Each refuses loud with no store and creates no file;
with a seeded store each writes as before.
Hermetic: the conftest isolation points BLOKPORT_CONFIG_DB at an absent path; seeded_config_store seeds a private one.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import decisions_store, store


def _membership():
    return [("Absolute Black", "granite", decisions_store._LEAF_ATTRIBUTES[0], "Black")]


def test_the_one_produce_opener_refuses_without_config_db_and_creates_no_file():
    path = store.config_db_path()
    with pytest.raises(store.ConfigStoreError, match="config.db absent"):
        store.open_existing_store()
    assert not path.exists()


def test_replace_pending_refuses_without_config_db_and_creates_no_file():
    path = store.config_db_path()
    with pytest.raises(store.ConfigStoreError, match="config.db absent"):
        decisions_store.replace_pending("variety", [])
    assert not path.exists()


def test_record_minted_membership_refuses_without_config_db_and_creates_no_file():
    path = store.config_db_path()
    with pytest.raises(store.ConfigStoreError, match="config.db absent"):
        decisions_store.record_minted_membership(_membership())
    assert not path.exists()


def test_with_a_seeded_store_each_produce_writer_writes(seeded_config_store):
    card = {"ref": "absolute black|granite", "payload": {"variant": "Absolute Black"}}
    decisions_store.replace_pending("decision_gap", [card])
    assert [p["variant"] for p in decisions_store.list_pending("decision_gap")] == ["Absolute Black"]
    assert decisions_store.record_minted_membership(_membership()) == 1


def test_the_config_server_boot_still_creates_the_store():
    # restore (absent here), then seed: the one creator
    path = store.config_db_path()
    assert store.seed_from_yaml() > 0 and path.exists()
