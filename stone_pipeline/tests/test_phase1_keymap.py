"""SC-1 union key-map generator: the engine (collapse, aborts, parse), the readers, and determinism."""
from __future__ import annotations

import csv
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from stone_pipeline.phase1 import keymap
from stone_pipeline.phase1.keymap import (Record, StorePaths, build_key_map, generate, load_type_vocab,
                                          parse_type_slug)
from stone_pipeline.phase1.variety_key import new_key

VOCAB = {"marble": {"Marble"}, "granite": {"Granite"}, "onyx": {"Onyx"},
         "dolomite": {"Dolomite"}, "dolomite_marble": {"Dolomite Marble"}}


def _led(env, key, type_name, name):
    return Record(env, "ledger", key, name=name, type_name=type_name)


# --- the collapse pins ----------------------------------------------------------------------------------
def test_live_copies_and_a_retired_copy_collapse_to_one_new_key():
    # slab/block/tile live in the ledger; the block copy is also marked retired (same Key string) -> one Key.
    recs = [
        _led("development", "slab_marble_absolute_black_u1", "Marble", "Absolute Black"),
        _led("development", "block_marble_absolute_black_u2", "Marble", "Absolute Black"),
        _led("development", "tile_marble_absolute_black_u3", "Marble", "Absolute Black"),
        Record("development", "config.retired", "block_marble_absolute_black_u2"),  # same Key, now retired
    ]
    res = build_key_map(recs, VOCAB)
    assert res.ok, res.aborts
    assert len(res.rows) == 3 and {m.new_key for m in res.rows} == {new_key("Marble", "Absolute Black")}
    assert sorted(m.category_rank for m in res.rows) == [0, 1, 2]


def test_three_retired_only_keys_collapse_to_one_new_key():
    # the Marble Revolution case: one whole identity retired, gone from the ledger, only config.db carries its
    # three Keys. They have no store row, so type and name come from the parse, and collapse to one new Key.
    recs = [Record("production", "config.retired", f"{b}_marble_revolution_u{i}")
            for i, b in enumerate(("slab", "block", "tile"))]
    res = build_key_map(recs, VOCAB)
    assert res.ok, res.aborts
    assert {m.new_key for m in res.rows} == {new_key("Marble", "Revolution")}
    assert sorted(m.category_rank for m in res.rows) == [0, 1, 2]


# --- aborts ---------------------------------------------------------------------------------------------
def test_bracket_identity_gets_one_clean_new_key():
    # The 33 bracket identities: every old Key carries the BRACKETED slug, but there is one clean Name
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


LIMESTONE = {"limestone": {"Limestone"}}


def _st_amber(prod_name):
    """One identity's three copies as prod held them at the read: the committed base, cleaned ('St Amber'),
    and prod's ledger written before it (`prod_name`)."""
    return [Record("production", "base_csv", f"{b}_limestone_st_amber_u{i}", name="St Amber", type_name="Limestone")
            for i, b in enumerate(("slab", "block", "tile"))] + [
            _led("production", f"{b}_limestone_st_amber_u{i}", "Limestone", prod_name)
            for i, b in enumerate(("slab", "block", "tile"))]


def test_a_spelling_only_difference_maps_to_its_one_new_key_and_is_listed():
    # 'St. Amber' and 'St Amber' slug alike (core.text.match_key folds punctuation and case), so both spellings give
    # ONE new Key: no name is chosen, since the map carries none. The difference is listed, never silent.
    res = build_key_map(_st_amber("St. Amber"), LIMESTONE)
    assert res.ok, res.aborts
    assert {m.new_key for m in res.rows} == {new_key("Limestone", "St Amber")} and len(res.rows) == 3
    assert res.spellings == ["limestone_st_amber (St Amber | St. Amber)"]


def test_a_different_name_on_one_identity_still_aborts_as_a_name_tie():
    # 'St Ambre' slugs apart from 'St Amber': two new Keys, an in-place rename, so the map aborts and lists it
    res = build_key_map(_st_amber("St Ambre"), LIMESTONE)
    copies = (f"{b}_limestone_st_amber_u{i}" for i, b in enumerate(("slab", "block", "tile")))
    assert res.aborts["name_tie"] == sorted(copies)
    assert res.spellings == []


def test_the_run_prints_the_spelling_only_differences_with_the_counts(tmp_path, monkeypatch, capsys):
    res = build_key_map(_st_amber("St. Amber"), LIMESTONE)
    monkeypatch.setattr(keymap, "generate", lambda stores, out_csv: (res, "digest"))
    assert keymap.main([], tmp_path) == 0
    out = capsys.readouterr().out
    assert "1 spelling-only" in out
    assert "  spelling (1): limestone_st_amber (St Amber | St. Amber)" in out


def test_two_protected_keys_to_one_new_key_abort():
    recs = [Record("development", "config.protected", "slab_marble_statuario_u1", protected=True),
            Record("development", "config.protected", "block_marble_statuario_u2", protected=True)]
    res = build_key_map(recs, VOCAB)
    assert not res.ok
    assert res.aborts["protected_collision"] == ["block_marble_statuario_u2", "slab_marble_statuario_u1"]


def test_type_conflict_names_both_sides():
    # the block post states another type than the Key. Keep the abort; list Key, Key's type,
    # the post's stated type.
    recs = [_led("development", "block_marble_panda_white_u1", "Marble", "Panda White"),   # the Key's type
            Record("production", "backbone", "block_marble_panda_white_u1", name="Panda White",
                   type_name="Granite")]                                                    # the post's type
    res = build_key_map(recs, VOCAB)
    assert not res.ok
    assert res.aborts["type_conflict"] == ["block_marble_panda_white_u1 (key=Marble, post=Granite)"]


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


# --- backbone spellings never tie or name ------------------------------------------------------------
def test_backbone_spelling_does_not_tie_or_name_the_key():
    # the store Name is authoritative; a backbone `variant` that differs only in case/accent must not tie, and
    # the new Key takes the STORE Name (162 identities tied on backbone drift alone).
    recs = [_led("development", "slab_andesite_nghe_an_volcanic_u1", "Andesite", "Nghe An Volcanic"),
            Record("development", "backbone", "slab_andesite_nghe_an_volcanic_u1", name="Nghe an Volcanic",
                   type_name="Andesite")]
    res = build_key_map(recs, {"andesite": {"Andesite"}})
    assert res.ok, res.aborts
    assert {m.new_key for m in res.rows} == {new_key("Andesite", "Nghe An Volcanic")}


def test_backbone_name_used_only_when_no_store_names_it():
    recs = [Record("development", "backbone", "slab_onyx_black_gold_u1", name="Black Gold", type_name="Onyx")]
    res = build_key_map(recs, VOCAB)
    assert res.ok and res.rows[0].new_key == new_key("Onyx", "Black Gold")
    assert res.ghosts == ["slab_onyx_black_gold_u1"]       # backbone-only -> mapped and listed as a ghost


# --- ghosts vs store collisions ----------------------------------------------------------------------
def test_backbone_only_collision_lists_ghosts_not_abort():
    # two keyed backbone posts no store carries, both slab, same identity -> ghosts, not an abort.
    recs = [Record("development", "backbone", "slab_granite_rosa_porrino_u1", name="Rosa Porrino",
                   type_name="Granite"),
            Record("development", "backbone", "slab_granite_rosa_porrino_u2", name="Rosa Porrino",
                   type_name="Granite")]
    res = build_key_map(recs, VOCAB)
    assert res.ok, res.aborts
    assert res.ghosts == ["slab_granite_rosa_porrino_u1", "slab_granite_rosa_porrino_u2"]


def test_two_store_keys_one_category_abort():
    # two STORE-backed Keys of one category on one new Key breaks the survivor pick -> abort.
    recs = [_led("development", "slab_granite_x_u1", "Granite", "X"),
            _led("development", "slab_granite_x_u2", "Granite", "X")]
    res = build_key_map(recs, VOCAB)
    assert not res.ok
    assert res.aborts["category_collision"] == ["slab_granite_x_u1", "slab_granite_x_u2"]


# --- a new Key from two middles is a merge -----------------------------------------------------------
def test_two_middles_one_new_key_listed_as_merged():
    # a suffixed copy joins its sibling by (type, Name): right, but two Medusa records become one.
    recs = [_led("development", "slab_granite_madam_red_b_u1", "Granite", "Madam Red"),
            _led("development", "tile_granite_madam_red_b_u2", "Granite", "Madam Red"),
            _led("development", "block_granite_madam_red_u3", "Granite", "Madam Red")]
    res = build_key_map(recs, VOCAB)
    assert res.ok, res.aborts
    assert res.merged == [new_key("Granite", "Madam Red")] and len(res.rows) == 3


# --- L1: malformed branch refused before indexing ----------------------------------------------------
def test_malformed_branch_refused_and_isolated():
    recs = [Record("development", "config.retired", "nounderscorekey"),
            Record("development", "config.retired", "widget_marble_x_u1"),   # 'widget' is not a pack category
            _led("development", "slab_marble_x_u2", "Marble", "X")]
    res = build_key_map(recs, VOCAB)
    assert res.aborts["unresolved_branch"] == ["nounderscorekey", "widget_marble_x_u1"]
    # the good slab Key is unaffected (the malformed ones did not pool into its identity)
    assert any(m.old_key == "slab_marble_x_u2" for m in res.rows)


def _full_snapshot_env(root: Path, env: str) -> Path:
    # create every file SNAPSHOT_STORES lists, so a complete snapshot builds; build_stores only checks presence.
    d = root / env
    for rels in keymap.SNAPSHOT_STORES.values():
        for rel in rels:
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
    return d


def test_build_stores_requires_every_listed_file_and_names_what_is_missing(tmp_path):
    from stone_pipeline.phase1.keymap import MissingStore, build_stores
    d = _full_snapshot_env(tmp_path, "development")
    stores = build_stores(tmp_path, envs=("development",))   # the complete snapshot builds
    assert stores[0].ledger_db and stores[0].base_copies != () and len(stores[0].backbone_files) == 3
    # remove each required file in turn, including a SINGLE file of a multi-file store; a missing file aborts
    # and is named (present == listed, no store is optional).
    for rel in ("ledger.db", "config.db", "variants_export_base.csv", "attributes.csv",
                "s3_base/variants_export_base.csv", "backbone_tiles.json",
                "backbone_additions/tile.json"):
        bak = d / (rel.replace("/", "_") + ".bak")
        (d / rel).rename(bak)
        with pytest.raises(MissingStore) as exc:
            build_stores(tmp_path, envs=("development",))
        assert rel in str(exc.value), f"{rel} not named: {exc.value}"
        bak.rename(d / rel)


def test_write_map_exact_bytes(tmp_path):
    from stone_pipeline.phase1.keymap import BRANCH_ORDER, KeyMapResult, MapRow, write_map
    assert [BRANCH_ORDER.index(b) for b in ("slab", "block", "tile")] == [0, 1, 2]   # 0-based pack order
    res = KeyMapResult(rows=[      # already in write order (new_key, category_rank, old_key)
        MapRow("slab_granite_y_u4", "granite__y_uuid", 0, "Granite, Special"),       # comma -> quoted
        MapRow("slab_marble_x_u1", "marble__x_uuid", 0, "Marble"),
        MapRow("block_marble_x_u2", "marble__x_uuid", 1, "Marble"),
        MapRow("tile_marble_x_u3", "marble__x_uuid", 2, "Marble"),
    ])
    out = tmp_path / "m.csv"
    digest = write_map(res, out)
    expected = (
        b"old_key,new_key,category_rank,type_name\n"
        b'slab_granite_y_u4,granite__y_uuid,0,"Granite, Special"\n'
        b"slab_marble_x_u1,marble__x_uuid,0,Marble\n"
        b"block_marble_x_u2,marble__x_uuid,1,Marble\n"
        b"tile_marble_x_u3,marble__x_uuid,2,Marble\n"
    )
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")                 # no BOM
    assert raw == expected                                     # \n ends, minimal quoting, exact header
    import hashlib
    assert digest == hashlib.sha256(expected).hexdigest()


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
        "category,value,sourceid,handle\ntype,Marble,1\ntype,Granite,2\ntype,Onyx,3\ncolor,Black,4\n", encoding="utf-8")
    return StorePaths(env=env, ledger_db=d / "ledger.db", config_db=d / "config.db", base_csv=d / "base.csv",
                      backbone_files=(d / "backbone.json",), backbone_additions=(d / "additions.json",),
                      attributes_csv=d / "attributes.csv")


def test_generate_end_to_end(tmp_path):
    stores = [_env_stores(tmp_path / "in", "development")]
    out = tmp_path / "out" / keymap.OUTPUT_CSV
    result, digest = generate(stores, out)
    assert result.ok, result.aborts
    assert result.untyped == ["block_avocatus_u7"]           # keyless backbone post ignored; untyped held
    assert result.ghosts == ["slab_onyx_black_gold_u8"]      # backbone-only Key: mapped and listed
    assert digest and out.exists()
    got = list(csv.DictReader(out.open(encoding="utf-8")))
    # three Marble copies (ledger slab+block, config retired tile) collapse to one new Key
    marble = [r for r in got if r["type_name"] == "Marble"]
    assert {r["new_key"] for r in marble} == {new_key("Marble", "Absolute Black")} and len(marble) == 3
    assert {r["type_name"] for r in got} == {"Marble", "Granite", "Onyx"}


def test_vocab_loads_only_type_rows(tmp_path):
    p = tmp_path / "attributes.csv"
    p.write_text("category,value,sourceid,handle\ntype,Marble,1\ncolor,Marble,2\ntype,Dolomite Marble,3\n",
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
