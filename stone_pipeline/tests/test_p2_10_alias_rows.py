"""P2-10 C4, the ledger and the payload (P2-10-SCRAPER-PLAN.md section 4; table 173 F9, table 189 C7): a variety serves
`alias_rows` [{value, method}] (method seed | operator | learned, ordered by match_key) and `codes` [string], the
scraper's WHOLE set on every serve; the old `aliases` field is gone. Ledger v19 replaces variation.aliases by alias_rows
and codes. NULL in either means "not computed yet", never "empty": '[]' would tell the backend the channel is empty
and delete every scraper alias, so a NULL row is HELD (never leased, stays dirty, counted by /status) until a produce
fills it (manager, 11-12). No em dashes."""
from __future__ import annotations

import json
import sqlite3

from stone_pipeline.aliases import payload as alias_payload
from stone_pipeline.ledger import sync
from stone_pipeline.ledger.db import SCHEMA_VERSION, Ledger
from stone_pipeline.ledger.payloads import variation_payload
from stone_pipeline.ledger.populate import populate_variations_full
from stone_pipeline.phase1.variety_key import new_key

CASA, GALAXY = new_key("Marble", "Casa Blue"), new_key("Granite", "Black Galaxy")
_ROWS = {CASA: {"alias_rows": [{"value": "Azul Casa", "method": "seed"}, {"value": "Casablue", "method": "learned"}],
                "codes": []},
         GALAXY: {"alias_rows": [{"value": "Galaxy Nero", "method": "operator"}], "codes": ["G603"]}}


def _variation(ledger, key, name, type_, *, state="dirty", alias_rows=None, codes=None):
    ledger.upsert("variation", {
        "key": key, "type": type_, "name": name, "medusa_id": None, "in_full": 1, "payload_hash": "h",
        "state": state, "first_seen": "t", "last_synced": None, "created_at": "t", "updated_at": "t",
        "alias_rows": None if alias_rows is None else json.dumps(alias_rows),
        "codes": None if codes is None else json.dumps(codes)}, pk=("key",))


def _full(tmp_path, sidecar: dict | None):
    """A produced 1_variants_full.csv (Aliases = the alias_rows values) and, when given, its alias sidecar."""
    path = tmp_path / "1_variants_full.csv"
    path.write_text("Key,Name,Type,Aliases\n"
                    f"{CASA},Casa Blue,Marble,Azul Casa|Casablue\n"
                    f"{GALAXY},Black Galaxy,Granite,Galaxy Nero\n", encoding="utf-8")
    if sidecar is not None:
        (tmp_path / alias_payload.ALIAS_SIDECAR).write_text(json.dumps(sidecar), encoding="utf-8")
    return path


# --- the migration ---------------------------------------------------------------------------------------------------
def test_a_v18_ledger_gains_null_alias_rows_and_codes_and_loses_aliases(tmp_path):
    p = tmp_path / "mig.ledger"
    with Ledger.open(p, env="development") as lg:
        _variation(lg, CASA, "Casa Blue", "Marble", alias_rows=[], codes=[])
    raw = sqlite3.connect(p)                                          # simulate a v18 ledger
    raw.execute("ALTER TABLE variation DROP COLUMN alias_rows")
    raw.execute("ALTER TABLE variation DROP COLUMN codes")
    raw.execute("ALTER TABLE variation ADD COLUMN aliases TEXT")
    raw.execute("UPDATE variation SET aliases = '[\"Azul Casa\"]'")
    raw.execute("PRAGMA user_version = 18")
    raw.execute("UPDATE ledger_meta SET schema_version = 18")
    raw.commit()
    raw.close()
    for _ in range(2):                                                # the second open is a no-op
        with Ledger.open(p, env="development") as lg:
            assert lg.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 19
            cols = {r["name"] for r in lg.conn.execute("PRAGMA table_info(variation)")}
            assert {"alias_rows", "codes"} <= cols and "aliases" not in cols
            row = lg.get("variation", "key", CASA)
            assert (row["alias_rows"], row["codes"], row["name"]) == (None, None, "Casa Blue")


# --- the hold: NULL is "not computed yet" ----------------------------------------------------------------------------
def test_a_pull_right_after_the_migration_serves_no_variation_without_its_alias_rows(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, CASA, "Casa Blue", "Marble")                                   # NULL, NULL
        _variation(lg, GALAXY, "Black Galaxy", "Granite", alias_rows=[], codes=None)  # codes still NULL
        assert sync.ready(lg, "variations") == []
        assert {lg.get("variation", "key", k)["state"] for k in (CASA, GALAXY)} == {"dirty"}   # never leased
        assert sync.status(lg)["variation_aliases"] == {"uncomputed": 2}


def test_an_empty_channel_is_served_as_empty(tmp_path):
    # '[]' is the contract's "this channel is empty": a variety with no scraper alias serves it, unlike NULL
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, CASA, "Casa Blue", "Marble", alias_rows=[], codes=[])
        served = sync.ready(lg, "variations")
        assert [(s["external_id"], s["payload"]["alias_rows"], s["payload"]["codes"]) for s in served] == [(CASA, [], [])]
        assert sync.status(lg)["variation_aliases"] == {"uncomputed": 0}


# --- the payload -----------------------------------------------------------------------------------------------------
def test_the_payload_carries_alias_rows_and_codes_and_no_aliases():
    v = {"type": "Granite", "name": "Black Galaxy", "alias_rows": json.dumps(_ROWS[GALAXY]["alias_rows"]),
         "codes": json.dumps(["G603"])}
    payload = variation_payload(v, [])
    assert "aliases" not in payload
    assert (payload["alias_rows"], payload["codes"]) == ([{"value": "Galaxy Nero", "method": "operator"}], ["G603"])


def test_alias_rows_and_codes_are_ordered_by_match_key():
    from stone_pipeline.aliases.surfaces import Surfaces
    rows, codes = alias_payload.rows_of(Surfaces(aliases=(("Zebra", "seed"), ("azul", "operator"), ("Hand", "admin")),
                                                 codes=(("G654", "seed"), ("g603", "learned"))))
    assert rows == [{"value": "azul", "method": "operator"}, {"value": "Zebra", "method": "seed"}]   # admin is not sent
    assert codes == ["g603", "G654"]


# --- the produce fills them: after one produce every variety serves its whole set -----------------------------------
def test_after_one_produce_every_variation_serves_its_whole_alias_rows(tmp_path):
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        _variation(lg, CASA, "Casa Blue", "Marble", state="synced")      # migrated: NULL, held
        _variation(lg, GALAXY, "Black Galaxy", "Granite", state="dirty")
        populate_variations_full(lg, _full(tmp_path, _ROWS))
        served = {s["external_id"]: s["payload"] for s in sync.ready(lg, "variations")}
        assert {k: (p["alias_rows"], p["codes"]) for k, p in served.items()} == {
            k: (v["alias_rows"], v["codes"]) for k, v in _ROWS.items()}


def test_a_populate_without_the_sidecar_leaves_the_rows_uncomputed(tmp_path):
    # a reset populates from the base, which has no sidecar: the rows stay held until a produce computes them
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        populate_variations_full(lg, _full(tmp_path, None))
        assert sync.ready(lg, "variations") == []
        assert sync.status(lg)["variation_aliases"] == {"uncomputed": 2}


def test_a_sidecar_missing_a_produced_variety_fails_loud(tmp_path):
    import pytest
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        with pytest.raises(ValueError, match="Black Galaxy"):
            populate_variations_full(lg, _full(tmp_path, {CASA: _ROWS[CASA]}))


# --- bootstrap: the export carries no scraper aliases after P2-7 -----------------------------------------------------
def test_the_bootstrap_seeds_variations_uncomputed(tmp_path):
    from stone_pipeline.ledger.bootstrap import seed_variations
    export = tmp_path / "variants_export.csv"
    export.write_text("Id,Key,Name,Image,AdminAliases,AdminCodes,status,Type,owner\n"
                      f"var_c,{CASA},Casa Blue,,Hand Casa,,active,Marble,\n", encoding="utf-8")
    with Ledger.open(tmp_path / "dev.ledger", env="development") as lg:
        assert seed_variations(lg, export) == 1
        row = lg.get("variation", "key", CASA)
        assert (row["alias_rows"], row["codes"], row["medusa_id"]) == (None, None, "var_c")


# --- the published base: no Aliases column, still a deliverable -----------------------------------------------------
def test_the_published_base_has_no_aliases_column(tmp_path):
    import csv
    from stone_pipeline.reference import sync_variants_base as svb
    base, deliverable = tmp_path / "base.csv", tmp_path / "to_upload" / "variants_export_base.csv"
    base.write_text(f"Key,Name,Type,Aliases\n{CASA},Casa Blue,Marble,Azul Casa|Casablue\n", encoding="utf-8")
    svb.write_deliverable(base, deliverable)
    with deliverable.open(encoding="utf-8", newline="") as h:
        assert list(csv.reader(h)) == [["Key", "Name", "Type"], [CASA, "Casa Blue", "Marble"]]


# --- emit: the ONE computation point (D1) ----------------------------------------------------------------------------
def test_emit_derives_aliases_and_the_sidecar_from_the_scraper_stores(tmp_path, monkeypatch):
    # the file's own Aliases are output, never read back; the hand rows are the backend's and never sent; the base's
    # Aliases are exactly the payload's alias_rows values
    import csv
    import dataclasses
    from stone_pipeline.aliases import stores as alias_stores
    from stone_pipeline.aliases.surfaces import Stores
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.stages import emit_catalog
    export = tmp_path / "variants_export.csv"
    export.write_text("Id,Key,Name,Image,AdminAliases,AdminCodes,status,Type,owner\n"
                      f"var_c,{CASA},Casa Blue,,Hand Casa,,active,Marble,\n", encoding="utf-8")
    paths = dataclasses.replace(SETTINGS.paths, review_dir=tmp_path / "review", variants_export_csv=export,
                                curate_delta_csv=tmp_path / "no_delta.csv")
    monkeypatch.setattr(emit_catalog, "SETTINGS", dataclasses.replace(SETTINGS, paths=paths))
    monkeypatch.setattr(alias_stores, "scraper_stores", lambda backbone=None: Stores(
        seed={CASA: (["Azul Casa"], ["G603"])}, learned=[(CASA, "Casablue")]))
    base = tmp_path / "variants_export_base.csv"
    base.write_text(f"Key,Name,Type,Aliases\n{CASA},Casa Blue,Marble,Base Only\n", encoding="utf-8")
    out = tmp_path / "out" / "1_variants_full.csv"
    out.parent.mkdir()
    emit_catalog.build(base, icons={}, out_path=out)
    with out.open(encoding="utf-8-sig", newline="") as h:
        assert [(r["Key"], r["Aliases"]) for r in csv.DictReader(h)] == [(CASA, "Azul Casa|Casablue")]
    sidecar = json.loads((out.parent / alias_payload.ALIAS_SIDECAR).read_text(encoding="utf-8"))
    assert sidecar == {CASA: {"alias_rows": [{"value": "Azul Casa", "method": "seed"},
                                             {"value": "Casablue", "method": "learned"}], "codes": ["G603"]}}
