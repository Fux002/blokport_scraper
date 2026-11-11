"""The material gate (DOMAIN_PACK.md): a NON-stone product run end to end through the real pipeline.

The scraper is built for any commodity; this proves it for wood. Ten wood records arrive through a load_frame source
(no website, no scrape file) and run through run_source and the catalog with SCRAPER_DOMAIN_PACK=wood, in a fresh
interpreter over a tmp copy of the workspace: the pack and the categories built from it at import are wood's own, every
path is wood's own (one material per deployment), and nothing reaches the network, AWS or a paid service.

Asserted, per the gate: nothing wrongly held, the right category per the pack's form roles, un-mangled names, correct
dimensions and density, a wood-only catalog, and no stone vocabulary in any emitted value (headers are the Medusa schema
and are excluded). A real stone assumption the run exposes is an xfail(strict=True) on exactly that assertion, naming
the file:line of its follow-up fix, so a fix flips it: never a weakened assertion."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import polars as pl
import pytest

from stone_pipeline.phase1.variety_key import variety_key_problem

FIXTURE = Path(__file__).parent / "fixtures" / "wood"
REPO = Path(__file__).resolve().parents[2]
FEED = json.loads((FIXTURE / "workspace" / "wood_feed.json").read_text(encoding="utf-8"))
DENSITY = {"Oak": 750}                 # reference/wood/type_density.csv; every other species takes the pack's default_density
PACK_DENSITY = 700
STONE_WORDS = re.compile(r"\b(slabs?|blocks?|tiles?|stones?|marble|granite|quartzite|onyx|travertine|limestone|"
                         r"dolomite|polished|honed|leathered|flamed|sandblasted)\b", re.IGNORECASE)


def build_workspace(root: Path) -> Path:
    """A wood deployment's workspace under `root`: the pipeline package (a real copy, so its paths, which derive from
    the package's own location, are the copy's; tests and caches excluded), the shared port list the ports fallback
    reads, the wood data (fixtures/wood/workspace overlaid), the load_frame adapter in the copy's adapters/ (where
    discovery finds it) and its source entry in the copy's sources.yaml. The repo itself is never written."""
    shutil.copytree(REPO / "stone_pipeline", root / "stone_pipeline",
                    ignore=shutil.ignore_patterns("tests", "__pycache__", "outputs", "*.db"))
    (root / "catalog_source").mkdir()
    shutil.copy(REPO / "catalog_source" / "ports.csv", root / "catalog_source" / "ports.csv")
    shutil.copytree(FIXTURE / "workspace", root, dirs_exist_ok=True)
    # The backend publishes field-kinds.json at run time; it is gitignored under from_medusa/, so the smoke run
    # stages the committed test fixture map into the wood instance, exactly as a produce would fetch it. Without
    # it the pipeline fails loud up front (field_kinds.require_present): correct, but not what this test exercises.
    (root / "from_medusa" / "development").mkdir(parents=True, exist_ok=True)
    shutil.copy(Path(__file__).parent / "fixtures" / "field-kinds.json",
                root / "from_medusa" / "development" / "field-kinds.json")
    shutil.copy(FIXTURE / "wood_feed.py", root / "stone_pipeline" / "adapters" / "wood_feed.py")
    sources = root / "stone_pipeline" / "config" / "sources.yaml"
    sources.write_text(sources.read_text(encoding="utf-8") + "\n" + (FIXTURE / "source.yaml").read_text(encoding="utf-8"),
                       encoding="utf-8")
    shutil.copy(FIXTURE / "smoke_run.py", root / "smoke_run.py")
    return root


def run_wood(root: Path, *, writethrough: bool = False) -> subprocess.CompletedProcess:
    """The smoke run in a fresh interpreter: wood pack, development tier, no credentials of any kind; ledger
    write-through on or off."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "FAL_", "SCRAPER_", "BLOKPORT_"))}
    env.update({"PYTHONPATH": str(root), "SCRAPER_DOMAIN_PACK": "wood", "SCRAPER_ENV": "development",
                "SCRAPER_S3_DRY_RUN": "true", "SCRAPER_CONFIG_DB": str(root / "config.db"),
                "SCRAPER_LEDGER_WRITETHROUGH": "1" if writethrough else "0",
                "SCRAPER_LEDGER_PATH": str(root / "wood.ledger"),
                "AWS_SHARED_CREDENTIALS_FILE": "/nonexistent", "AWS_CONFIG_FILE": "/nonexistent",
                "AWS_EC2_METADATA_DISABLED": "true"})
    return subprocess.run([sys.executable, "smoke_run.py"], cwd=root, env=env, capture_output=True, text=True,
                          timeout=600)


def _completed(done: subprocess.CompletedProcess) -> None:
    assert done.returncode == 0, f"wood smoke run failed (rc {done.returncode}):\n{done.stderr[-4000:]}"


def _csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _sku(record: dict) -> str:
    return f"WDF-{record['product_id']}".upper()


@pytest.fixture(scope="module")
def wood(tmp_path_factory) -> Path:
    root = build_workspace(tmp_path_factory.mktemp("wood"))
    _completed(run_wood(root))
    return root


@pytest.fixture(scope="module")
def products(wood) -> dict[str, dict]:
    """The emitted wood products by SKU."""
    return {r["Variant Sku"]: r for r in _csv(wood / "to_upload" / "development" / "3_products_woodfeed.csv")}


@pytest.mark.xfail(strict=True, reason="fix owner: the Phase 1 data model (blokport_research/2026-11-03-attribute-"
                   "logistics-audit/PHASE-1-SPEC.md:110 one variety backbone checked against the default form, and :74 D9 "
                   "`finishes_from`): reconcile validates every category against the DEFAULT form's backbone only "
                   "(reference/loaders/reference_data.py:162, read at stages/reconcile_tree.py:127), so a beam's 'Raw' "
                   "finish is checked against the plank tree and every bulk-form product is held as missing_leaf_child. "
                   "Stone keeps that rule on purpose: per-category backbones would newly hold 42 varsha 'Polished' blocks")
def test_every_feed_row_is_emitted_nothing_wrongly_held(wood, products):
    rejects = [r for f in (wood / "stone_pipeline" / "outputs" / "development").glob("*/review/products_rejects.csv")
               for r in _csv(f)]
    assert rejects == [] and set(products) == {_sku(r) for r in FEED}


def test_each_product_lands_in_its_forms_category(wood, products):
    pcat = {r["value"]: r["sourceid"] for r in _csv(wood / "from_medusa" / "development" / "attributes.csv")
            if r["category"] == "category"}
    plural = {"plank": "Planks", "beam": "Beams", "panel": "Panels"}
    by_sku = {_sku(r): r for r in FEED}
    assert products and all(row["Product Category 1"] == pcat[plural[by_sku[sku]["form"]]] for sku, row in products.items())
    canonical = pl.read_parquet(next((wood / "stone_pipeline" / "outputs" / "development").glob(
        "*/diagnostics/canonical.parquet")))
    # the pack's bulk_form (beam) is the only form that takes the is_block branch
    assert {r["raw_format"]: r["is_block"] for r in canonical.iter_rows(named=True)} == {
        "plank": False, "beam": True, "panel": False}


def test_names_are_kept_unmangled(products):
    by_sku = {_sku(r): r for r in FEED}
    for sku, row in products.items():
        fed = by_sku[sku]
        assert row["Product Title"] == f"{fed['name']} {fed['finish']}"
        assert row["STN Type Name"] == fed["species"]
        assert row["STN Variation Id"].startswith(f"{fed['form']}_{fed['species'].lower()}_"
                                                  f"{fed['name'].lower().replace(' ', '_')}_")


def test_dimensions_and_density_are_wood(products):
    by_sku = {_sku(r): r for r in FEED}
    for sku, row in products.items():
        fed = by_sku[sku]
        length, width, height = float(fed["length"]), float(fed["width"]), float(fed["thickness"].split()[0])
        assert (float(row["Product Length"]), float(row["Product Width"]), float(row["Product Height"])) == (
            length, width, height)
        density = DENSITY.get(fed["species"], PACK_DENSITY)
        assert float(row["Product Weight"]) == round(length * width * height * density / 1000.0, 3)


def test_the_catalog_carries_only_wood(wood, products):
    upload = wood / "to_upload" / "development"
    variants = _csv(upload / "1_variants_full.csv")
    assert variants and {v["Name"] for v in variants} == {r["name"] for r in FEED}
    assert all(variety_key_problem(v["Key"]) is None for v in variants)        # phase 1 Keys: one per variety
    combos = _csv(upload / "2_valid_combinations.csv")
    wood_pcats = {r["sourceid"] for r in _csv(wood / "from_medusa" / "development" / "attributes.csv")
                  if r["category"] == "category"}
    assert combos and {c["product_category_id"] for c in combos} <= wood_pcats
    assert {row["Product Category 1"] for row in products.values()} <= {c["product_category_id"] for c in combos}


def test_no_stone_vocabulary_in_any_emitted_value(wood):
    files = [*(wood / "to_upload" / "development").glob("*.csv"), *(wood / "review" / "development").glob("*.csv"),
             *(wood / "stone_pipeline" / "outputs" / "development").glob("*/*/*.csv")]
    leaks = {(f.name, col, m.group(0)) for f in files for row in _csv(f) for col, value in row.items()
             for m in STONE_WORDS.finditer(value or "")}
    for doc in (wood / "to_upload" / "development" / "standardise_photo_list.json",
                wood / "image_pipeline" / "prompts_to_generate.json"):
        leaks |= {(doc.name, "", m.group(0)) for m in STONE_WORDS.finditer(doc.read_text(encoding="utf-8"))}
    assert len(files) >= 10 and leaks == set()


def test_the_per_material_data_stays_in_the_packs_own_folders(wood):
    # the package ships stone's synonyms (flat), untouched, and the wood run (the fixture) never read them: wood reads
    # only its own reference/wood/ (here its densities; it has no synonyms)
    reference = wood / "stone_pipeline" / "reference"
    synonyms = reference / "synonyms"
    assert (synonyms / "finish.csv").read_bytes() == (REPO / "stone_pipeline" / "reference" / "synonyms" /
                                                      "finish.csv").read_bytes()
    assert not (reference / "wood" / "synonyms").exists()
    # the catalog's new-variety deltas land in wood's own catalog_source folder, never the stone flat one
    assert any((wood / "catalog_source" / "wood" / "backbone_additions").glob("*.json"))
    assert not (wood / "catalog_source" / "backbone_additions").exists()


@pytest.mark.xfail(strict=True, reason="fix owner: the Phase 1 ledger schema bump (blokport_research/2026-11-03-"
                   "attribute-logistics-audit/PHASE-1-SPEC.md:249 drops `branch` and its CHECK): today the ledger's "
                   "variation.branch only admits the stone categories (ledger/schema.sql:61), so seeding a wood variety "
                   "fails and write-through cannot record a wood product")
def test_the_ledger_records_the_wood_products(tmp_path):
    root = build_workspace(tmp_path)
    _completed(run_wood(root, writethrough=True))
    emitted = {r["Variant Sku"] for r in _csv(root / "to_upload" / "development" / "3_products_woodfeed.csv")}
    with sqlite3.connect(root / "wood.ledger") as ledger:
        recorded = {sku for (sku,) in ledger.execute("SELECT sku FROM product")}
    assert emitted and recorded == emitted
