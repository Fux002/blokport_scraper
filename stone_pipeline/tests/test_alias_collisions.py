"""An alias never equals the name of another variety of the SAME type (Mas, 11-11 and 11-12): the rule on small cases;
CI holds the WHOLE committed vocabulary (both bases and the backbones) at 0; and at emit an alias that collides with a
live variety (an operator mint equal to a seed alias) is DROPPED and LISTED, the produce never stops. No em dashes."""
from __future__ import annotations

import csv
import dataclasses
import json
from pathlib import Path

from stone_pipeline.reference import alias_collisions as ac

REPO = Path(__file__).resolve().parents[2]
BASES = sorted((REPO / "from_medusa").glob("*/variants_export_base.csv"))
BACKBONES = [REPO / "catalog_source" / "backbone_varieties.json",
             REPO / "catalog_source" / "backbone_additions" / "varieties.json"]


def _v(key, stone_type, name, *aliases):
    return ac.Variety(key, stone_type, name, tuple(aliases))


def test_an_alias_naming_another_variety_collides():
    found = ac.find([_v("m1", "Marble", "Brazilian White", "White Super"), _v("m2", "Marble", "White Super")])
    assert [(c.alias, c.variety, c.collides_with) for c in found] == [
        ("White Super", ("Marble", "Brazilian White"), ("Marble", "White Super"))]
    assert "'White Super' of Marble 'Brazilian White'" in str(found[0])


def test_its_own_name_and_its_own_copies_are_not_collisions():
    # the same identity in two categories, and an alias that only repeats the name with its type word
    assert ac.find([_v("slab_q", "Quartzite", "Super White", "Super White Quartzite"),
                    _v("block_q", "Quartzite", "Super White", "Super White Quartzite")]) == []


def test_a_post_under_the_same_key_is_the_same_variety_not_another():
    # Scraper M2: the base row 'G562 Maple Red' and its backbone post spelt 'G562' share one Key; the post's spelling
    # is that variety's own name, so the base row's alias 'G562' is not a collision
    key = "granite__g562_maple_red_8a0f2c3e-1b2c-5d6e-8f90-a1b2c3d4e5f6"
    assert ac.find([_v(key, "Granite", "G562 Maple Red", "G562", "G562 Granite"), _v(key, "Granite", "G562")]) == []


def test_the_same_name_under_another_type_is_not_a_collision():
    # a granite 'Silver Grey' alias and a slate 'Silver Grey' variety are two stones matching keeps apart (Mas, 11-11)
    assert ac.find([_v("g1", "Granite", "Bianco Sardo", "Silver Grey"), _v("s1", "Slate", "Silver Grey")]) == []


def _committed():
    out = []
    for base in BASES:
        with base.open(encoding="utf-8-sig", newline="") as h:
            for r in csv.DictReader(h):
                out.append(_v(r["Key"], r["Type"], r["Name"], *[a for a in (r["Aliases"] or "").split("|") if a]))
    for path in BACKBONES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for p in (doc["posts"] if isinstance(doc, dict) else doc):
            if p.get("stone_type"):                       # an untyped addition is held, never a variety
                out.append(_v(p.get("key") or f"{path.name}:{p['variant']}", p["stone_type"], p["variant"],
                              *p.get("aliases", [])))
    return out


def test_no_committed_alias_names_another_variety():
    collisions = ac.find(_committed())
    assert not collisions, (f"{len(collisions)} committed alias(es) name another variety of the same type:\n"
                            + "\n".join(str(c) for c in collisions))


def _seed(monkeypatch, seed: dict) -> None:
    """The alias stores emit reads (P2-10 C4: a variety's aliases come from the stores, never from the base file): the
    given backbone seed, for a produce build and for the hermetic seed projection alike."""
    from stone_pipeline.aliases import stores as alias_stores
    from stone_pipeline.aliases.surfaces import Stores
    from stone_pipeline.reference.loaders import backbone
    monkeypatch.setattr(alias_stores, "scraper_stores", lambda backbone=None: Stores(seed=seed))
    monkeypatch.setattr(alias_stores, "seed_of", lambda _backbone: seed)
    monkeypatch.setattr(backbone, "load_backbone", lambda path=None: backbone.Backbone())


def test_an_alias_equal_to_a_minted_variety_is_dropped_and_listed_and_the_produce_completes(tmp_path, monkeypatch):
    # the seed's marble Casa Blue carries 'Calcite Blue'; the operator mints marble 'Calcite Blue' (curate's delta). The
    # emit keeps both varieties, drops the colliding alias from Casa Blue, and lists it for the operator.
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.phase1.variety_key import new_key
    from stone_pipeline.stages import emit_catalog
    review = tmp_path / "review"
    monkeypatch.setattr(emit_catalog, "SETTINGS",
                        dataclasses.replace(SETTINGS, paths=dataclasses.replace(SETTINGS.paths, review_dir=review)))
    casa, calcite = new_key("Marble", "Casa Blue"), new_key("Marble", "Calcite Blue")
    _seed(monkeypatch, {casa: (["Calcite Blue", "Casa Blue Marble"], [])})
    base, delta = tmp_path / "variants_export_base.csv", tmp_path / "curate_delta.csv"
    for path, rows in ((base, [[casa, "Casa Blue", "Marble", ""]]),
                       (delta, [[calcite, "Calcite Blue", "Marble", ""]])):
        with path.open("w", newline="", encoding="utf-8") as h:
            csv.writer(h).writerows([emit_catalog._COLS, *rows])
    real_rows = emit_catalog._rows
    monkeypatch.setattr(emit_catalog, "_rows",
                        lambda p: real_rows(delta) if Path(p) == SETTINGS.paths.curate_delta_csv else real_rows(p))
    out = tmp_path / "out" / "1_variants_full.csv"
    out.parent.mkdir()
    emit_catalog.build(base, icons={}, out_path=out)
    with out.open(encoding="utf-8-sig", newline="") as h:
        emitted = {r["Key"]: r for r in csv.DictReader(h)}
    assert set(emitted) == {casa, calcite}
    assert emitted[casa]["Aliases"].split("|") == ["Casa Blue Marble"]
    with (review / emit_catalog.ALIAS_COLLISIONS_REVIEW).open(encoding="utf-8-sig") as h:
        listed = list(csv.DictReader(h))
    assert [(r["Key"], r["alias"], r["collides_with_key"]) for r in listed] == [(casa, "Calcite Blue", calcite)]
    assert emit_catalog.alias_collisions_dropped() == 1        # the count the run summary states


def _emit_with_a_collision(tmp_path, monkeypatch, *, stamp, collide=True, seed_only=False):
    """One build over a base with marble Casa Blue (its seed carrying 'Calcite Blue') and, when `collide`, marble
    Calcite Blue in the same base; the review dir is tmp and the per-run stamp is `stamp`. Returns (emit_catalog, review dir)."""
    from stone_pipeline.config.settings import SETTINGS
    from stone_pipeline.phase1.variety_key import new_key
    from stone_pipeline.stages import emit_catalog
    review = tmp_path / "review"
    paths = dataclasses.replace(SETTINGS.paths, review_dir=review, variants_export_csv=tmp_path / "no_export.csv",
                                curate_delta_csv=tmp_path / "no_delta.csv")   # no live export, no delta
    monkeypatch.setattr(emit_catalog, "SETTINGS", dataclasses.replace(SETTINGS, paths=paths))
    monkeypatch.setattr(emit_catalog, "_run_stamp", lambda: stamp)
    base = tmp_path / f"base_{stamp}.csv"
    _seed(monkeypatch, {new_key("Marble", "Casa Blue"): (["Calcite Blue", "Casa Blue Marble"], [])})
    rows = [[new_key("Marble", "Casa Blue"), "Casa Blue", "Marble", ""]]
    if collide:
        rows.append([new_key("Marble", "Calcite Blue"), "Calcite Blue", "Marble", ""])
    with base.open("w", newline="", encoding="utf-8") as h:
        csv.writer(h).writerows([emit_catalog._COLS, *rows])
    out = tmp_path / f"out_{stamp}.csv"
    emit_catalog.build(base, icons={}, seed_only=seed_only, out_path=out)
    return emit_catalog, review


def _listed(path):
    with path.open(encoding="utf-8-sig", newline="") as h:
        return [r["alias"] for r in csv.DictReader(h)]


def test_every_run_s_dropped_list_survives_the_next_run(tmp_path, monkeypatch):
    # the base keeps only the emitted aliases (base := 1_variants_full), so a dropped alias is gone for good: each
    # run's list is kept under its own name, while the current list holds the latest run's (Scraper L2)
    emit_catalog, review = _emit_with_a_collision(tmp_path, monkeypatch, stamp="20261112T010000Z")
    emit_catalog, review = _emit_with_a_collision(tmp_path, monkeypatch, stamp="20261112T020000Z", collide=False)
    runs = review / emit_catalog.ALIAS_COLLISIONS_RUNS
    assert sorted(p.name for p in runs.iterdir()) == ["20261112T010000Z.csv"]   # a run that drops none adds none
    assert _listed(runs / "20261112T010000Z.csv") == ["Calcite Blue"]
    assert _listed(review / emit_catalog.ALIAS_COLLISIONS_REVIEW) == []


def test_the_seed_projection_drops_but_never_rewrites_the_review_list(tmp_path, monkeypatch):
    # seed.verify's hermetic rebuild (seed_only) must leave the produce's review list as it is (Scraper L1)
    from stone_pipeline.stages import emit_catalog
    review = tmp_path / "review"
    review.mkdir()
    (review / emit_catalog.ALIAS_COLLISIONS_REVIEW).write_text("the produce's list\n", encoding="utf-8")
    emit_catalog, review = _emit_with_a_collision(tmp_path, monkeypatch, stamp="20261112T030000Z", seed_only=True)
    assert (review / emit_catalog.ALIAS_COLLISIONS_REVIEW).read_text(encoding="utf-8") == "the produce's list\n"
    assert not (review / emit_catalog.ALIAS_COLLISIONS_RUNS).exists()
    with (tmp_path / "out_20261112T030000Z.csv").open(encoding="utf-8-sig", newline="") as h:
        aliases = {r["Name"]: r["Aliases"] for r in csv.DictReader(h)}
    assert aliases["Casa Blue"] == "Casa Blue Marble"                 # the projection still drops it


def test_the_run_summary_states_the_dropped_aliases_and_where_they_are_listed(tmp_path, monkeypatch):
    from stone_pipeline.config.settings import ENV_NAME, SETTINGS
    from stone_pipeline.core import layout
    from stone_pipeline.stages import emit_catalog
    monkeypatch.setattr(layout, "SETTINGS",
                        dataclasses.replace(SETTINGS, paths=dataclasses.replace(SETTINGS.paths, to_upload_dir=tmp_path)))
    text = layout.write_sync_md(counts={}, sources=[], products={}, aliases_dropped=2).read_text(encoding="utf-8")
    assert f"2 alias(es) dropped at emit, each the name of another variety of the same type: " \
           f"`review/{ENV_NAME}/{emit_catalog.ALIAS_COLLISIONS_REVIEW}`" in text


def test_the_catalog_passes_the_dropped_alias_count_to_the_run_summary(tmp_path, monkeypatch):
    import types
    from stone_pipeline import catalog
    from stone_pipeline.reference import sync_variants_base
    stated: dict = {}
    noop = lambda *a, **k: None  # noqa: E731
    zero = lambda *a, **k: 0  # noqa: E731
    monkeypatch.setattr(catalog, "find_canonical", lambda root: [tmp_path / "x.parquet"])
    monkeypatch.setattr(catalog.staging, "read_canonical", lambda p: [])
    monkeypatch.setattr(catalog.loaders, "load_all", noop)
    monkeypatch.setattr(catalog.curate, "run", lambda rows, ref: types.SimpleNamespace(counts={}))
    monkeypatch.setattr("stone_pipeline.stages.decisions.heal_drifted_membership", noop)
    monkeypatch.setattr(catalog, "collect_products", lambda root: {})
    monkeypatch.setattr(catalog.emit_catalog, "build", noop)
    monkeypatch.setattr(catalog.emit_catalog, "alias_collisions_dropped", lambda: 3)
    for name in ("collect_inventory", "collect_discontinued", "collect_lot_cards", "write_variants_to_delete",
                 "migrate_retyped_variant_images", "drop_deleted_variants", "prune_superseded_runs"):
        monkeypatch.setattr(catalog, name, zero)
    monkeypatch.setattr(catalog, "write_sync_md", lambda **k: stated.update(k) or tmp_path / "SYNC_STEPS.md")
    monkeypatch.setattr(catalog.prepare_variant_images, "queue_new_textures", zero)
    monkeypatch.setattr("stone_pipeline.stages.variety_color.fill_colors", lambda **k: {})
    monkeypatch.setattr(sync_variants_base, "sync", noop)
    monkeypatch.setattr(sync_variants_base, "write_deliverable", noop)
    for name in ("_record_catalog", "_consistency_gate"):
        monkeypatch.setattr(catalog, name, noop)
    monkeypatch.setattr(catalog.tree_build, "run", noop)
    catalog.run(tmp_path)
    assert stated["aliases_dropped"] == 3
