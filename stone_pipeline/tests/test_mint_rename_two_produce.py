"""Mint + rename across TWO produces, through the real matcher and the real existing-variety index.

Produce 1: the scrape gaps on 'Honey Onyx'; the operator's mint decision carries the corrected name 'Honey'.
           curate mints 'Honey' (Onyx) with 'Honey Onyx' as its alias and emits the row.
Boundary:  Medusa pulls the new variety and re-exports variants_export.csv (the ONE file both the matcher and
           curate read -- loaders.existing_varieties_file). Modelled here by appending the emitted row, with a
           Medusa id, to the export -- exactly the contract every alias decision already depends on.
Produce 2: the same scrape must resolve onto the 'Honey' Key through the alias surface (no gap, no re-mint),
           and curate must not propose anything new for it.
"""

from __future__ import annotations

import csv
from pathlib import Path
from types import SimpleNamespace

from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.reference import loaders
from stone_pipeline.stages import curate, decisions, match_variation, normalize, reconcile_tree
from stone_pipeline.tests.test_mint_type_authority import _typed_gap_row

EXPORT_FIELDS = ["Id", "Key", "Name", "Image", "Aliases", "Volume per kg (m³/kg)"]
EXISTING_KEY = "slab_granite_alpine_00000000-0000-0000-0000-000000000001"


def _paths(tmp_path: Path) -> SimpleNamespace:
    real = loaders.SETTINGS.paths
    # every public attribute, INCLUDING computed properties (backbone_json ...), which vars() would miss
    return SimpleNamespace(**{**{n: getattr(real, n) for n in dir(real) if not n.startswith("_")},
                              "export_file": tmp_path / "variants_export.csv",
                              "variants_export_base_csv": tmp_path / "variants_export_base.csv"})


def _write_export(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as h:
        w = csv.DictWriter(h, fieldnames=EXPORT_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({f: r.get(f, "") for f in EXPORT_FIELDS})


def _read_export(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as h:
        return list(csv.DictReader(h))


def _scrape_row(key: str) -> CanonicalRow:
    return CanonicalRow(src_site="zucchi", surrogate_key=key, variety_match_key="Honey Onyx",
                        raw_format="Slab", raw_color="Yellow", raw_finish="Polished", raw_quality="A",
                        raw_type="Onyx")


def _match(rows: list[CanonicalRow], ref) -> None:
    normalize.run(rows, ref)
    match_variation.run(rows, ref)
    reconcile_tree.run(rows, ref)


def test_renamed_mint_binds_the_product_on_the_next_produce(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=paths))
    monkeypatch.setattr(curate, "_alias_model", lambda *a, **k: (None, {}))
    # an unrelated existing variety, so the index is a real (non-empty) export
    _write_export(paths.export_file, [{"Id": "var_alpine", "Key": EXISTING_KEY, "Name": "Alpine",
                                       "Aliases": "Alpine Granite"}])

    # -- produce 1: the scrape gaps, the operator mints it as 'Honey' --------------------------------------
    ref1 = loaders.load_all()
    rows1 = [_scrape_row("p1")]
    _match(rows1, ref1)
    assert rows1[0].variation_key is None, "must gap before the mint exists"

    monkeypatch.setattr(decisions, "load_confirm_decisions", lambda: {("", "honey onyx"): "yes"})
    monkeypatch.setattr(decisions, "load_variety_seed_names", lambda: {("", "honey onyx"): "Honey"})
    res1 = curate.build_curation([_typed_gap_row("Honey Onyx", "Onyx")], ref1)
    minted = res1.new_variants["slab"]
    assert [r["Name"] for r in minted] == ["Honey"]
    honey_key = minted[0]["Key"]
    assert honey_key.startswith("slab_onyx_honey_")
    assert "Honey Onyx" in minted[0]["Aliases"].split("|")

    # -- boundary: Medusa pulls the variety and re-exports it (Id assigned, Aliases carried) ----------------
    _write_export(paths.export_file, _read_export(paths.export_file) + [
        {"Id": "var_honey", "Key": honey_key, "Name": "Honey", "Aliases": minted[0]["Aliases"]}])

    # -- produce 2: the same scrape binds through the alias surface, nothing is proposed again -------------
    ref2 = loaders.load_all()
    rows2 = [_scrape_row("p1")]
    _match(rows2, ref2)
    assert rows2[0].variation_key == honey_key
    assert rows2[0].variation_id == "var_honey"
    assert rows2[0].variation_name == "Honey"
    assert not rows2[0].tree_gaps

    res2 = curate.build_curation(rows2, ref2)
    assert res2.new_variants["slab"] == []                     # no re-mint: the decision is spent
    assert all(a.get("_status") != "pending" for a in res2.alias_additions["slab"])


def test_operator_api_decision_drives_the_renamed_mint_end_to_end(tmp_path, monkeypatch):
    # No monkeypatched decision loaders: the operator's PUT lands in a real config.db, the pending card is
    # real, and curate reads the store exactly as a produce does.
    from stone_pipeline.config import server, varieties
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    paths = _paths(tmp_path)
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=paths))
    monkeypatch.setattr(curate, "_alias_model", lambda *a, **k: (None, {}))
    monkeypatch.setattr(varieties, "_rows", lambda q=None: [{"name": "Alpine", "stone_type": "Granite"}])
    _write_export(paths.export_file, [{"Id": "var_alpine", "Key": EXISTING_KEY, "Name": "Alpine"}])
    ref = loaders.load_all()

    # produce 1 surfaces the card ...
    res0 = curate.build_curation([_typed_gap_row("Honey Onyx", "Onyx")], ref)
    assert res0.new_variants["slab"] == []
    decisions.write_confirm_file(res0.pending_confirm)        # what write_curation does at the end of a produce
    _, listing = server.dispatch("GET", ["review", "variants"], None)
    card = next(v for v in listing["variants"] if v["variant"] == "Honey Onyx")
    assert card["stone_type"].lower() == "onyx" and card["current_seed_name"] is None

    # ... the operator states what the zucchi listing IS: 'Honey', an Onyx (a rename, since neither exists) ...
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "zucchi", "scraped": "Honey Onyx", "name": "Honey", "type": "Onyx"})
    assert code == 200 and body["result"] == "minted", body
    assert decisions.load_variety_seed_names() == {("", "honey onyx"): "Honey"}

    # ... produce 2 mints 'Honey' under the corrected name, reading the real store ...
    res1 = curate.build_curation([_typed_gap_row("Honey Onyx", "Onyx")], ref)
    minted = res1.new_variants["slab"]
    assert [r["Name"] for r in minted] == ["Honey"]
    honey_key = minted[0]["Key"]
    assert honey_key.startswith("slab_onyx_honey_")
    # the card is gone: the decision is spent (the produce rewrites the queue without it)
    decisions.write_confirm_file(res1.pending_confirm)
    _, listing = server.dispatch("GET", ["review", "variants"], None)
    assert all(v["variant"] != "Honey Onyx" for v in listing["variants"])

    # ... Medusa pulls it and re-exports ...
    _write_export(paths.export_file, _read_export(paths.export_file) + [
        {"Id": "var_honey", "Key": honey_key, "Name": "Honey", "Aliases": minted[0]["Aliases"]}])

    # ... produce 3: the first mint on a spelling is its GLOBAL meaning: the zucchi product binds to 'Honey'
    # through the alias the rename attached for everyone, and so does another vendor's identical spelling.
    ref3 = loaders.load_all()
    zucchi, other = _scrape_row("p1"), _scrape_row("p2").model_copy(update={"src_site": "polonine"})
    _match([zucchi, other], ref3)
    assert zucchi.variation_key == honey_key and zucchi.variation_id == "var_honey"
    assert not zucchi.tree_gaps
    assert other.variation_key == honey_key and not other.tree_gaps
    assert curate.build_curation([zucchi], ref3).new_variants["slab"] == []   # nothing proposed again


def test_renamed_mint_is_idempotent_while_the_pull_lags(tmp_path, monkeypatch):
    # If produce 2 runs BEFORE Medusa has re-exported (the export still lacks 'Honey'), the row gaps again
    # and curate mints the same Key with the same alias -- byte-identical output, no drift, no duplicate.
    paths = _paths(tmp_path)
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=paths))
    monkeypatch.setattr(curate, "_alias_model", lambda *a, **k: (None, {}))
    _write_export(paths.export_file, [{"Id": "var_alpine", "Key": EXISTING_KEY, "Name": "Alpine"}])
    monkeypatch.setattr(decisions, "load_confirm_decisions", lambda: {("", "honey onyx"): "yes"})
    monkeypatch.setattr(decisions, "load_variety_seed_names", lambda: {("", "honey onyx"): "Honey"})
    ref = loaders.load_all()
    first = curate.build_curation([_typed_gap_row("Honey Onyx", "Onyx")], ref).new_variants
    second = curate.build_curation([_typed_gap_row("Honey Onyx", "Onyx")], ref).new_variants
    assert first == second
    assert [r["Key"] for r in first["slab"]] == [r["Key"] for r in second["slab"]]


def test_a_matched_product_is_re_pointed_by_a_scoped_mint_rename(tmp_path, monkeypatch):
    """The override fix, end-to-end through the REAL matcher. A scrape the matcher binds to an EXISTING
    variety (no gap) is re-pointed to a NEW variety the operator mints+renames it to, FOR ONE vendor:
      produce 1 -- the scrape matches 'Brown Granite'; the operator states marenostone's is 'Chocolate
                   Classic'; curate mints Chocolate Classic DESPITE the match (the fix);
      produce 2 -- after the re-export, marenostone's product binds to Chocolate Classic via the vendor-
                   scoped alias, while another vendor's identical spelling keeps the Brown Granite match.
    This is exactly the prod decision_gap (brown granite -> Chocolate Classic) the audit kept reporting."""
    from stone_pipeline.config import server, varieties
    monkeypatch.setenv("BLOKPORT_CONFIG_DB", str(tmp_path / "config.db"))
    paths = _paths(tmp_path)
    monkeypatch.setattr(loaders, "SETTINGS", SimpleNamespace(paths=paths))
    monkeypatch.setattr(curate, "_alias_model", lambda *a, **k: (None, {}))
    monkeypatch.setattr(varieties, "_rows", lambda q=None: [{"name": "Brown Granite", "stone_type": "Granite"}])
    BG_KEY = "slab_granite_brown_granite_00000000-0000-0000-0000-000000000002"
    _write_export(paths.export_file, [{"Id": "var_bg", "Key": BG_KEY, "Name": "Brown Granite"}])

    def _bg(key="p1", src="marenostone"):
        return CanonicalRow(src_site=src, surrogate_key=key, variety_match_key="Brown Granite",
                            raw_format="Slab", raw_color="Brown", raw_type="Granite")

    # produce 1: the scrape MATCHES the existing Brown Granite (no gap) ...
    ref1 = loaders.load_all()
    row1 = _bg()
    _match([row1], ref1)
    assert row1.variation_key == BG_KEY and not row1.tree_gaps, "premise: it matches the existing variety"

    # ... the operator states marenostone's 'Brown Granite' is really a NEW 'Chocolate Classic' ...
    code, body = server.dispatch("PUT", ["review", "decide"],
                                 {"source": "marenostone", "scraped": "Brown Granite",
                                  "name": "Chocolate Classic", "type": "Granite"})
    assert code == 200 and body["result"] == "minted", body

    # ... curate mints Chocolate Classic DESPITE the match (the override fix) ...
    res1 = curate.build_curation([_bg()], ref1)
    minted = res1.new_variants["slab"]
    assert [r["Name"] for r in minted] == ["Chocolate Classic"], minted
    cc_key = minted[0]["Key"]
    assert cc_key.startswith("slab_granite_chocolate_classic_"), cc_key

    # boundary: Medusa re-exports Chocolate Classic ...
    _write_export(paths.export_file, _read_export(paths.export_file) + [
        {"Id": "var_cc", "Key": cc_key, "Name": "Chocolate Classic", "Aliases": minted[0].get("Aliases", "")}])

    # produce 2: marenostone's product re-points to Chocolate Classic via the vendor-scoped alias;
    # another vendor's identical spelling is NOT re-pointed (the statement was marenostone-only).
    ref2 = loaders.load_all()
    mine, other = _bg("p1", "marenostone"), _bg("p2", "polonine")
    _match([mine, other], ref2)
    assert mine.variation_key == cc_key, f"marenostone must re-point to Chocolate Classic, got {mine.variation_key}"
    assert mine.variation_name == "Chocolate Classic"
    assert other.variation_key == BG_KEY, f"another vendor keeps Brown Granite, got {other.variation_key}"
