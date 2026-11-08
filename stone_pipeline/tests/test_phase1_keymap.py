"""SC-1 union key-map generator: the engine (collapse, aborts, parse), the readers, and determinism."""
from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from stone_pipeline.phase1 import keymap
from stone_pipeline.phase1.keymap import (Record, StorePaths, build_key_map, generate, load_type_vocab,
                                          parse_type_slug)
from stone_pipeline.phase1.variety_key import new_key

VOCAB = {"marble": {"Marble"}, "granite": {"Granite"}, "onyx": {"Onyx"},
         "dolomite": {"Dolomite"}, "dolomite_marble": {"Dolomite Marble"}}


def _led(env, key, type_name, name):
    return Record(env, "ledger", key, name=name, type_name=type_name)


# --- the collapse pin (manager's required test) ---------------------------------------------------------
def test_identity_copies_and_retired_only_key_collapse_to_one_new_key():
    recs = [
        _led("development", "slab_marble_absolute_black_u1", "Marble", "Absolute Black"),
        _led("development", "block_marble_absolute_black_u2", "Marble", "Absolute Black"),
        _led("development", "tile_marble_absolute_black_u3", "Marble", "Absolute Black"),
        Record("production", "config.retired", "block_marble_absolute_black_u4"),  # no row: resolved by parse
    ]
    res = build_key_map(recs, VOCAB)
    assert res.ok, res.aborts
    assert len(res.rows) == 4
    assert {m.new_key for m in res.rows} == {new_key("Marble", "Absolute Black")}
    assert sorted(m.category_rank for m in res.rows) == [0, 1, 1, 2]   # slab, block, block(retired), tile
    assert {m.type_name for m in res.rows} == {"Marble"}


# --- aborts ---------------------------------------------------------------------------------------------
def test_bracket_identity_gets_one_clean_new_key():
    # The 33 bracket identities (rev 8): every old Key carries the BRACKETED slug, but there is one clean Name
    # per copy. The new Key must use the clean Name, so all copies and a retired-only copy collapse to ONE Key.
    recs = [
        _led("development", "slab_granite_absolute_black_nero_assoluto_u1", "Granite", "Absolute Black"),
        _led("production", "block_granite_absolute_black_nero_assoluto_u2", "Granite", "Absolute Black"),
        Record("production", "config.retired", "tile_granite_absolute_black_nero_assoluto_u3"),  # no row
    ]
    res = build_key_map(recs, VOCAB)
    assert res.ok, res.aborts
    new_keys = {m.new_key for m in res.rows}
    assert new_keys == {new_key("Granite", "Absolute Black")} and len(res.rows) == 3
    # NOT the bracketed-slug Key that parsing the old Key's own slug would have minted
    assert new_key("Granite", "absolute black nero assoluto") not in new_keys


def test_bracketed_store_name_is_cleaned_for_the_new_key():
    recs = [_led("development", "slab_granite_g562_maple_red_u1", "Granite", "G562 (Maple Red)")]
    res = build_key_map(recs, VOCAB)
    assert res.ok and res.rows[0].new_key == new_key("Granite", "G562")


def test_name_tie_across_envs_aborts():
    recs = [_led("development", "slab_marble_x_u1", "Marble", "Absolute Black"),
            _led("production", "slab_marble_x_u1", "Marble", "Absolute Blk")]
    res = build_key_map(recs, VOCAB)
    assert not res.ok and res.aborts["name_tie"] == ["slab_marble_x_u1"]


def test_two_protected_keys_to_one_new_key_abort():
    recs = [Record("development", "config.protected", "slab_marble_statuario_u1", protected=True),
            Record("development", "config.protected", "block_marble_statuario_u2", protected=True)]
    res = build_key_map(recs, VOCAB)
    assert not res.ok
    assert res.aborts["protected_collision"] == ["block_marble_statuario_u2", "slab_marble_statuario_u1"]


def test_type_conflict_on_one_key_aborts():
    recs = [_led("development", "slab_marble_x_u1", "Marble", "X"),
            _led("production", "slab_marble_x_u1", "Granite", "X")]
    res = build_key_map(recs, VOCAB)
    assert not res.ok and res.aborts["type_conflict"] == ["slab_marble_x_u1"]


def test_unresolvable_rowless_type_aborts():
    recs = [Record("development", "config.retired", "slab_unknowntype_x_u1")]
    res = build_key_map(recs, VOCAB)
    assert not res.ok and res.aborts["unresolved_type"] == ["slab_unknowntype_x_u1"]


def test_type_absent_from_medusa_vocab_aborts():
    recs = [_led("development", "slab_basalt_x_u1", "Basalt", "X")]   # 'basalt' not in VOCAB
    res = build_key_map(recs, VOCAB)
    assert not res.ok and res.aborts["unresolved_type"] == ["slab_basalt_x_u1"]


def test_ambiguous_vocab_slug_aborts():
    recs = [_led("development", "slab_quartz_x_u1", "Quartz", "X")]
    res = build_key_map(recs, {"quartz": {"Quartz", "Quartz "}})   # two names, one slug
    assert not res.ok and res.aborts["unresolved_type"] == ["slab_quartz_x_u1"]


# --- untyped + parse ------------------------------------------------------------------------------------
def test_untyped_addition_is_listed_not_mapped():
    recs = [Record("development", "backbone_additions", "block_avocatus_u1", name="Avocatus", untyped=True)]
    res = build_key_map(recs, VOCAB)
    assert res.ok and res.rows == [] and res.untyped == ["block_avocatus_u1"]


def test_rowless_base_key_resolves_by_parse():
    recs = [Record("development", "base_csv", "slab_granite_tan_brown_u1", name="Tan Brown")]
    res = build_key_map(recs, VOCAB)
    assert res.ok and len(res.rows) == 1
    assert res.rows[0].new_key == new_key("Granite", "Tan Brown") and res.rows[0].type_name == "Granite"


def test_prefix_type_longest_match():
    assert parse_type_slug("slab_dolomite_marble_perla_u1", set(VOCAB)) == ("dolomite_marble", "perla")
    assert parse_type_slug("slab_dolomite_perla_u1", set(VOCAB)) == ("dolomite", "perla")
    recs = [Record("development", "base_csv", "slab_dolomite_marble_perla_u1", name="Perla"),
            Record("development", "base_csv", "slab_dolomite_perla_u2", name="Perla")]
    res = build_key_map(recs, VOCAB)
    assert res.ok
    assert {m.new_key for m in res.rows} == {new_key("Dolomite Marble", "Perla"), new_key("Dolomite", "Perla")}


# --- determinism ----------------------------------------------------------------------------------------
def test_engine_is_order_independent():
    recs = [_led("development", "slab_marble_absolute_black_u1", "Marble", "Absolute Black"),
            _led("development", "block_granite_tan_brown_u2", "Granite", "Tan Brown"),
            Record("development", "config.retired", "tile_marble_absolute_black_u3")]
    a = build_key_map(recs, VOCAB)
    b = build_key_map(list(reversed(recs)), VOCAB)
    assert [(m.old_key, m.new_key, m.category_rank, m.type_name) for m in a.rows] == \
           [(m.old_key, m.new_key, m.category_rank, m.type_name) for m in b.rows]


# --- readers + integration ------------------------------------------------------------------------------
def _make_ledger(path, rows):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE variation (key TEXT PRIMARY KEY, type TEXT, name TEXT)")
    con.executemany("INSERT INTO variation (key, type, name) VALUES (?,?,?)", rows)
    con.commit(); con.close()


def _make_config_db(path, retired, protected):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE retired_variation (key TEXT PRIMARY KEY, created_at TEXT)")
    con.execute("CREATE TABLE protected_variation (key TEXT PRIMARY KEY, decided_at TEXT)")
    con.executemany("INSERT INTO retired_variation VALUES (?, 't')", [(k,) for k in retired])
    con.executemany("INSERT INTO protected_variation VALUES (?, 't')", [(k,) for k in protected])
    con.commit(); con.close()


def _env_stores(root: Path, env: str) -> StorePaths:
    d = root / env
    d.mkdir(parents=True, exist_ok=True)
    _make_ledger(d / "ledger.db", [("slab_marble_absolute_black_u1", "Marble", "Absolute Black"),
                                   ("block_marble_absolute_black_u2", "Marble", "Absolute Black")])
    _make_config_db(d / "config.db", retired=["tile_marble_absolute_black_u3"], protected=[])
    (d / "base.csv").write_text("Key,Name,Image,Aliases\nslab_granite_tan_brown_u9,Tan Brown,,\n",
                                encoding="utf-8")
    (d / "backbone.json").write_text(json.dumps({"posts": [
        {"key": "slab_onyx_black_gold_u8", "variant": "Black Gold", "stone_type": "Onyx"},
        {"key": "", "variant": "Keyless", "stone_type": "Onyx"}]}), encoding="utf-8")
    (d / "additions.json").write_text(json.dumps([
        {"key": "block_avocatus_u7", "variant": "Avocatus", "stone_type": ""}]), encoding="utf-8")
    (d / "attributes.csv").write_text(
        "category,value,sourceid\ntype,Marble,1\ntype,Granite,2\ntype,Onyx,3\ncolor,Black,4\n", encoding="utf-8")
    return StorePaths(env=env, ledger_db=d / "ledger.db", config_db=d / "config.db", base_csv=d / "base.csv",
                      backbone_files=(d / "backbone.json",), backbone_additions=(d / "additions.json",),
                      attributes_csv=d / "attributes.csv")


def test_generate_end_to_end(tmp_path):
    stores = [_env_stores(tmp_path / "in", "development")]
    out = tmp_path / "out" / keymap.OUTPUT_CSV
    result, digest = generate(stores, out)
    assert result.ok, result.aborts
    assert result.untyped == ["block_avocatus_u7"]           # keyless backbone post ignored; untyped held
    assert digest and out.exists()
    got = list(csv.DictReader(out.open(encoding="utf-8")))
    # three Marble copies (ledger slab+block, config retired tile) collapse to one new Key
    marble = [r for r in got if r["type_name"] == "Marble"]
    assert {r["new_key"] for r in marble} == {new_key("Marble", "Absolute Black")} and len(marble) == 3
    assert {r["type_name"] for r in got} == {"Marble", "Granite", "Onyx"}


def test_vocab_loads_only_type_rows(tmp_path):
    p = tmp_path / "attributes.csv"
    p.write_text("category,value,sourceid\ntype,Marble,1\ncolor,Marble,2\ntype,Dolomite Marble,3\n",
                 encoding="utf-8")
    vocab = load_type_vocab([p])
    assert vocab == {"marble": {"Marble"}, "dolomite_marble": {"Dolomite Marble"}}


def test_two_hashseeds_give_byte_identical_csv(tmp_path):
    stores_root = tmp_path / "in"
    _env_stores(stores_root, "development")
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from stone_pipeline.phase1.keymap import StorePaths, generate, OUTPUT_CSV\n"
        "d = Path(%r) / 'development'\n"
        "s = [StorePaths(env='development', ledger_db=d/'ledger.db', config_db=d/'config.db',\n"
        "     base_csv=d/'base.csv', backbone_files=(d/'backbone.json',),\n"
        "     backbone_additions=(d/'additions.json',), attributes_csv=d/'attributes.csv')]\n"
        "out = Path(sys.argv[1])\n"
        "generate(s, out)\n"
        "sys.stdout.write(out.read_text())\n"
    ) % (str(Path.cwd()), str(stores_root))
    import os
    outs = []
    for seed in ("0", "17"):
        p = tmp_path / f"map_{seed}.csv"
        env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
        env["PYTHONHASHSEED"] = seed
        r = subprocess.run([sys.executable, "-c", script, str(p)], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        outs.append(p.read_bytes())
    assert outs[0] == outs[1]
