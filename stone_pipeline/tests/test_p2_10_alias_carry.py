"""P2-10 C4, the acked alias set survives ledger v18 (reviewer 2's blocking; Mas's ruling, 13 Nov).

v18 adds alias_rows and codes (NULL: not computed yet, held from the serve) and no longer drops the old `aliases`. THE
conversion is a one-time step at the produce's start (aliases.carry, after the learned-alias migration, before any
build): in ONE ledger transaction it gives every NULL row's old values their owner method through the produce's own
derivation (aliases.derive.alias_sets over the stores: operator over seed over learned, a code split into codes),
verifies no NULL row is left in the whole ledger, and drops `aliases`. A value no store owns is a refusal, never a
default: the ledger is left as it was, the Keys and values are named, and the produce stops. A ledger without
`aliases` is done (the step skips and says so). A dry run takes the same path and writes nothing. No em dashes."""
from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from stone_pipeline import produce
from stone_pipeline.aliases import cleanup
from stone_pipeline.aliases.surfaces import Stores
from stone_pipeline.core.alias_problem import code_matcher
from stone_pipeline.core.text import match_key
from stone_pipeline.ledger import sync
from stone_pipeline.ledger.db import Ledger, now_iso
from stone_pipeline.phase1.variety_key import new_key

ABS, KASH, CARR = (new_key("Granite", "Absolute Black"), new_key("Granite", "Kashmir White"),
                   new_key("Marble", "Carrara"))
IS_CODE = code_matcher(r"[Gg]\d+[A-Za-z]?")
SEED = {ABS: (["Nero Assoluto"], ["G654"]), KASH: (["Bianco Kashmir"], []), CARR: (["Bianco Carrara"], [])}


@pytest.fixture(autouse=True)
def _vocabulary(monkeypatch):
    """C2's one context builder (aliases.context), on a fixed vocabulary instead of this env's attributes export."""
    from stone_pipeline.aliases import context
    monkeypatch.setattr(context, "_clear_type_words", lambda: frozenset({"granite", "marble"}))
    monkeypatch.setattr(context, "vocabulary_keys", lambda: frozenset({"black", "white", "granite", "marble"}))


def _row(ledger, key, name, type_, *, in_full=1, state="synced"):
    now = now_iso()
    ledger.upsert("variation", {"key": key, "type": type_, "name": name, "medusa_id": "V-" + key[:6],
                                "in_full": in_full, "payload_hash": "h", "state": state, "first_seen": now,
                                "last_synced": now, "created_at": now, "updated_at": now}, pk=("key",))


def _v17(path, aliases: dict[str, list[str]]):
    """A v17 ledger (before C4): variation carries `aliases`, no alias_rows or codes. Three rows: KASH in the produced
    set, ABS export-only (in_full=0, synced), CARR retiring with its pending variation tombstone."""
    with Ledger.open(path, env="development") as lg:
        _row(lg, KASH, "Kashmir White", "Granite")
        _row(lg, ABS, "Absolute Black", "Granite", in_full=0)
        _row(lg, CARR, "Carrara", "Marble", state="retiring")
        now = now_iso()
        lg.upsert("removed", {"external_id": CARR, "kind": "variation", "source": None, "reason": "variation_removed",
                              "state": "pending", "sync_attempts": 0, "created_at": now, "updated_at": now},
                  pk=("external_id",))
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE variation DROP COLUMN alias_rows")
    raw.execute("ALTER TABLE variation DROP COLUMN codes")
    raw.execute("ALTER TABLE variation ADD COLUMN aliases TEXT")
    for key, values in aliases.items():
        raw.execute("UPDATE variation SET aliases = ? WHERE key = ?", (json.dumps(values), key))
    raw.execute("PRAGMA user_version = 17")
    raw.execute("UPDATE ledger_meta SET schema_version = 17")
    raw.commit()
    raw.close()


OLD = {KASH: ["Bianco Kashmir"], ABS: ["G654", "Nero Assoluto"], CARR: ["Bianco Carrara"]}


def _columns(ledger):
    return {r["name"] for r in ledger.execute("PRAGMA table_info(variation)")}


def _c5_stores():
    return cleanup.Stores(seed={k: frozenset(match_key(v) for v in a + c) for k, (a, c) in SEED.items()},
                          operator={}, learned={}, is_code=IS_CODE)


def _carry(ledger, seed=SEED, **kw):
    from stone_pipeline.aliases import carry
    return carry.run(ledger, stores=Stores(seed=seed), is_code=IS_CODE, **kw)


# --- the ledger step -----------------------------------------------------------------------------------------------------
def test_v18_adds_alias_rows_and_codes_and_keeps_the_old_set_for_the_carry(tmp_path):
    _v17(tmp_path / "l.db", OLD)
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        assert {"alias_rows", "codes", "aliases"} <= _columns(lg)
        assert json.loads(lg.get("variation", "key", ABS)["aliases"]) == ["G654", "Nero Assoluto"]


def test_between_the_roll_and_the_carry_no_null_row_serves_and_the_file_refuses(tmp_path):
    _v17(tmp_path / "l.db", OLD)
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        lg.execute("UPDATE variation SET state = 'dirty' WHERE key = ?", (KASH,))
        assert sync.ready(lg, "variations") == []                       # D2: a NULL row is held
        lg.execute("UPDATE variation SET state = 'synced' WHERE key = ?", (KASH,))
        with pytest.raises(cleanup.NotAllComputed):
            cleanup.classify(lg, _c5_stores())


# --- the carry --------------------------------------------------------------------------------------------------------
def test_an_export_only_and_a_retiring_row_classify_after_the_carry_with_the_pre_c4_bytes(tmp_path):
    _v17(tmp_path / "l.db", OLD)
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        report = _carry(lg)
        assert report["unownable"] == [] and report["rows"] == 3
        assert "aliases" not in _columns(lg)
        assert lg.execute("SELECT COUNT(*) FROM variation WHERE alias_rows IS NULL OR codes IS NULL").fetchone()[0] == 0
        abs_row = lg.get("variation", "key", ABS)
        assert (json.loads(abs_row["alias_rows"]), json.loads(abs_row["codes"])) == (
            [{"value": "Nero Assoluto", "method": "seed"}], ["G654"])
        assert cleanup.csv_bytes(cleanup.classify(lg, _c5_stores())) == (
            f"Key,Value,Class\n{ABS},G654,code\n{ABS},Nero Assoluto,seed\n{KASH},Bianco Kashmir,seed\n"
            f"{CARR},Bianco Carrara,seed\n").encode("utf-8")


def test_an_unownable_value_refuses_naming_it_and_leaves_the_ledger_as_it_was(tmp_path):
    from stone_pipeline.aliases import carry
    _v17(tmp_path / "l.db", {**OLD, ABS: ["Nero Assoluto", "Mystery Stone"]})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        with pytest.raises(carry.CarryRefused, match="Mystery Stone") as refused:
            _carry(lg)
        assert refused.value.unownable == [(ABS, "Mystery Stone")]
        assert "aliases" in _columns(lg)
        assert lg.execute("SELECT COUNT(*) FROM variation WHERE alias_rows IS NULL").fetchone()[0] == 3


def test_a_refused_carry_is_retried_whole_and_a_done_one_skips_and_says_so(tmp_path, caplog):
    _v17(tmp_path / "l.db", {**OLD, ABS: ["Nero Assoluto", "Jet Black"]})
    from stone_pipeline.aliases import carry
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        with pytest.raises(carry.CarryRefused):
            _carry(lg)
        owned = {**SEED, ABS: (["Nero Assoluto", "Jet Black"], ["G654"])}  # the owner's store now names it
        assert _carry(lg, seed=owned)["rows"] == 3
        with caplog.at_level(logging.INFO):
            assert _carry(lg, seed=owned) is None
        assert "already carried" in caplog.text


def test_a_dry_run_takes_the_same_path_and_writes_nothing(tmp_path):
    _v17(tmp_path / "l.db", {**OLD, ABS: ["Nero Assoluto", "Mystery Stone"]})
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        report = _carry(lg, dry_run=True)
        assert report["unownable"] == [[ABS, "Mystery Stone"]] and report["rows"] == 3
        assert "aliases" in _columns(lg)
        assert lg.execute("SELECT COUNT(*) FROM variation WHERE alias_rows IS NULL").fetchone()[0] == 3


def test_the_method_is_the_owner_operator_over_seed_over_learned(tmp_path):
    _v17(tmp_path / "l.db", {**OLD, ABS: ["Nero Assoluto", "Jet Black", "Shanxi Black"]})
    from stone_pipeline.aliases.surfaces import OperatorAlias
    stores = Stores(seed={**SEED, ABS: (["Nero Assoluto", "Shanxi Black"], ["G654"])},
                    operator=[OperatorAlias("Absolute Black", "Granite", "Nero Assoluto")],
                    learned=[(ABS, "Shanxi Black"), (ABS, "Jet Black")])
    from stone_pipeline.aliases import carry
    with Ledger.open(tmp_path / "l.db", env="development") as lg:
        carry.run(lg, stores=stores, is_code=IS_CODE)
        assert json.loads(lg.get("variation", "key", ABS)["alias_rows"]) == [
            {"value": "Jet Black", "method": "learned"}, {"value": "Nero Assoluto", "method": "operator"},
            {"value": "Shanxi Black", "method": "seed"}]


# --- the produce ------------------------------------------------------------------------------------------------------
def _produce_calls(monkeypatch, carry_effect):
    from stone_pipeline.aliases import carry, migrate_learned
    from stone_pipeline.ledger import snapshot, writethrough
    calls = []
    monkeypatch.setattr(produce, "_fetch_inputs", lambda: calls.append("fetch"))
    monkeypatch.setattr(produce, "_mirror_archived", lambda: calls.append("mirror"))
    monkeypatch.setattr(writethrough, "enabled", lambda: True)
    monkeypatch.setattr(migrate_learned, "run_once", lambda **k: calls.append("learned"))
    monkeypatch.setattr(carry, "run_produce_step", lambda: calls.append("carry") or carry_effect())
    monkeypatch.setattr(produce, "_live_scrape", lambda s: calls.append("scrape") or 0)
    monkeypatch.setattr(produce.build, "run", lambda sources, stage: calls.append("build") or 1)
    monkeypatch.setattr(produce, "_finalize_control_plane", lambda stage: None)
    monkeypatch.setattr(snapshot, "save_artifacts", lambda *a, **k: None)
    return calls


def test_the_produce_carries_after_the_learned_move_and_before_any_build(monkeypatch):
    calls = _produce_calls(monkeypatch, lambda: None)
    produce.run(None, "inventory")
    assert calls == ["fetch", "mirror", "learned", "carry", "build"]


def test_a_refused_carry_stops_the_produce_before_it_writes(monkeypatch):
    from stone_pipeline.aliases import carry

    def refuse():
        raise carry.CarryRefused([(ABS, "Mystery Stone")])
    calls = _produce_calls(monkeypatch, refuse)
    with pytest.raises(carry.CarryRefused):
        produce.run(None, "inventory")
    assert calls == ["fetch", "mirror", "learned", "carry"]


# --- the payload: one spelling per (Key, alias_key) (34, 13 Nov) ---------------------------------------------------------
def test_the_payload_carries_one_spelling_per_key_and_alias_key_the_first_by_owner_then_store_order():
    # the rule: the first by method precedence (operator, seed, learned), then the store's own order; a second
    # spelling of the same alias_key is never sent, so the backend never chooses between two
    from stone_pipeline.aliases import derive
    from stone_pipeline.aliases.surfaces import OperatorAlias
    rows = [{"Key": ABS, "Name": "Absolute Black", "Type": "Granite"}]
    seed = {ABS: (["Rosé", "rose", "ROSE"], [])}
    aliases, _, _, _ = derive.alias_sets(rows, Stores(seed=seed), IS_CODE, lambda v: v)
    assert aliases[ABS] == [("Rosé", "seed")]
    stores = Stores(seed=seed, operator=[OperatorAlias("Absolute Black", "Granite", "ROSE")])
    aliases, _, _, _ = derive.alias_sets(rows, stores, IS_CODE, lambda v: v)
    assert aliases[ABS] == [("ROSE", "operator")]
