"""Wave 5a: vocabulary lives in the domain pack, not in code.

Supplier inventory prefixes, trailing render tags, colour spelling variants, join-noise words, the
colour-like attribute, and the in/out-of-stock words were stone-shaped constants inside matching/ and
derive.py; the ECS launch names were blokport literals. Each is a pack field (or the brand) now, and the
three packs declare them; a pack that declares none strips and recognises nothing extra.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest
import yaml

from stone_pipeline.config import domain
from stone_pipeline.core.schema import CanonicalRow
from stone_pipeline.matching import engine, projections as proj
from stone_pipeline.stages import derive


def _pack_with(monkeypatch, **overrides):
    pack = dataclasses.replace(domain.active_pack(), **overrides)
    monkeypatch.setattr(domain, "active_pack", lambda: pack)
    return pack


def test_a_pack_declared_inventory_prefix_is_stripped_before_matching(monkeypatch):
    _pack_with(monkeypatch, inventory_prefixes=("q",), trailing_render_tags=("oiled",))
    assert proj.deprefixed("Q Rosal / oiled") == "rosal"
    assert proj.deprefixed("Z Rosal") == "z rosal"            # stone's prefix is not this pack's


def test_a_pack_declared_spelling_variant_unifies_colour_words(monkeypatch):
    from stone_pipeline.adapters import tokens
    monkeypatch.setattr(tokens, "known_values", lambda kind: ["Grey", "Gray", "Beige"])
    _pack_with(monkeypatch, spelling_variants={"gray": "grey"})
    assert engine._colour_conflict("Pietra Gray", "Pietra Grey") is False
    _pack_with(monkeypatch, spelling_variants={})
    assert engine._colour_conflict("Pietra Gray", "Pietra Grey") is True


def test_pack_stock_words_drive_the_availability_flags(monkeypatch):
    _pack_with(monkeypatch, in_stock_words=("op voorraad",), out_of_stock_words=("uitverkocht",))
    assert derive._in_stock_word(CanonicalRow(src_site="x", surrogate_key="1", raw_stock_status="Op Voorraad"))
    assert derive._out_of_stock_flag(CanonicalRow(src_site="x", surrogate_key="2", raw_stock_status="uitverkocht"))
    assert not derive._in_stock_word(CanonicalRow(src_site="x", surrogate_key="3", raw_stock_status="in stock"))


def _stone_data():
    return yaml.safe_load(domain._pack_path("stone").read_text(encoding="utf-8"))


@pytest.mark.parametrize("field,value,needle", [
    ("finish_phrases", ["Polished", "Honed"], "finish_phrases"),
    ("default_finishes", [], "default_finishes"),
    ("generic_material_word", "", "generic_material_word"),
])
def test_malformed_pack_fields_fail_loud_naming_the_field(field, value, needle):
    data = _stone_data()
    data[field] = value
    with pytest.raises(ValueError, match=needle):
        domain._validate_shape("stone", domain._pack_path("stone"), data)


def test_ecs_launch_names_derive_from_the_brand(monkeypatch):
    from stone_pipeline.config import runner
    seen = {}

    class _Ecs:
        def run_task(self, **kw):
            seen.update(kw)
            return {"tasks": [{"taskArn": "arn:x/abc"}]}
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Ecs())
    monkeypatch.setattr(runner, "BRAND", "wudport")
    monkeypatch.delenv("SCRAPER_ECS_CONTAINER", raising=False)
    monkeypatch.delenv("SCRAPER_ECS_TASKDEF", raising=False)
    monkeypatch.delenv("BLOKPORT_ECS_CONTAINER", raising=False)
    monkeypatch.delenv("BLOKPORT_ECS_TASKDEF", raising=False)
    monkeypatch.setenv("SCRAPER_ECS_CLUSTER", "c")
    monkeypatch.setenv("SCRAPER_ECS_SUBNETS", "subnet-1")
    monkeypatch.setenv("SCRAPER_ECS_SG", "sg-1")
    runner._launch_ecs({"run_id": "r", "stage": "all", "status": "queued"})
    assert seen["taskDefinition"].startswith("wudport-scraper-")
    assert seen["overrides"]["containerOverrides"][0]["name"].startswith("wudport-scraper-")


@pytest.mark.parametrize("pack", ["stone", "wood", "lime"])
def test_every_pack_loads_and_drives_the_shared_code_in_a_fresh_process(pack):
    code = (
        "from stone_pipeline.config import domain; p = domain.active_pack(); assert p.name == %r, p.name\n"
        "from stone_pipeline.matching import projections as proj, engine\n"
        "from stone_pipeline.adapters.tokens import clean_variety\n"
        "from stone_pipeline.stages import derive\n"
        "proj.deprefixed('Sample Name 3cm'); engine._colour_conflict('a b', 'a c'); clean_variety('Sample Name', '')\n"
        "print('ok', p.name, sorted(p.inventory_prefixes), p.color_attribute)\n" % pack)
    env = {"SCRAPER_DOMAIN_PACK": pack, "PATH": "", "HOME": "/nonexistent", "PYTHONPATH": "."}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.startswith(f"ok {pack}")
