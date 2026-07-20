"""Phase 3: the product-domain pack. The stone pack reproduces the historical hardcoded constants exactly
(byte-parity is proven end-to-end by the spine/emit tests); the loader fails loud on a missing/invalid pack,
never silently substituting a default vocabulary.
"""

from __future__ import annotations

import pytest

from stone_pipeline.config import domain


def test_stone_pack_loads_exact_values():
    p = domain.load_pack("stone")
    assert p.name == "stone"
    assert p.attributes == ("type", "color", "finish", "quality")     # normalize.VOCAB_FIELDS order
    assert p.disambiguator == "type"
    assert p.leaf_attributes == ("color", "finish", "quality")
    assert p.ambiguous_type_words == frozenset({"crystal", "quartz", "agate", "amethyst", "coral"})


def test_consumers_read_the_pack_not_hardcoded():
    # the wiring is proven: the stage-level vocab constants ARE the pack's, so a different pack (selected by
    # BLOKPORT_DOMAIN_PACK at startup) changes them everywhere with no code edit.
    from stone_pipeline.reference.loaders import VOCAB_CATEGORIES
    from stone_pipeline.stages.normalize import VOCAB_FIELDS
    from stone_pipeline.config.decisions_store import _LEAF_ATTRIBUTES
    p = domain.active_pack()
    assert VOCAB_FIELDS == tuple(p.attributes)
    assert set(VOCAB_CATEGORIES) == set(p.attributes)
    assert _LEAF_ATTRIBUTES == tuple(p.leaf_attributes)
    assert p.default_finishes == ("Polished", "Honed", "Leathered", "Brushed", "Flamed",
                                  "Sandblasted", "Sawn Cut", "Raw")
    assert p.fallback_color == "Natural"
    assert p.dimension_ranges["slab"]["weight"] == (0.225, 0.350)
    assert p.dimension_ranges["block"]["weight"] == (18.0, 23.0)
    assert p.dimension_ranges["tile"]["height"] == (0.3, 0.6)
    assert p.finish_phrases["polished"] == \
        "a bright, mirror-like surface that reflects light and deepens the stone's colour"
    assert p.finish_phrase_default == "a refined natural surface"


def test_active_pack_defaults_to_stone(monkeypatch):
    monkeypatch.delenv("BLOKPORT_DOMAIN_PACK", raising=False)
    domain.active_pack.cache_clear()
    assert domain.active_pack().name == "stone"


def test_pack_default_value_absent_from_attributes_fails_loud(monkeypatch):
    # the enforceable names-vs-values boundary: a pack default VALUE that is not a real Medusa value in
    # attributes.csv must fail LOUD at reference load, not ship as an unresolvable null id downstream.
    import dataclasses
    from types import SimpleNamespace
    from stone_pipeline.reference import loaders
    bogus = dataclasses.replace(domain.active_pack(), block_finish="Zzz Not A Real Finish")
    monkeypatch.setattr(domain, "active_pack", lambda: bogus)
    ref = SimpleNamespace(attributes=loaders.load_attributes())
    with pytest.raises(ValueError, match="attributes.csv"):
        loaders._assert_pack_defaults_resolve(ref)


def test_missing_pack_fails_loud():
    with pytest.raises(FileNotFoundError):
        domain.load_pack("does_not_exist_pack")


def test_missing_key_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    (tmp_path / "broken.yaml").write_text("name: broken\n", encoding="utf-8")
    with pytest.raises(ValueError):
        domain.load_pack("broken")


def _valid_pack_dict():
    import copy
    return copy.deepcopy({
        "name": "bad", "attributes": ["a", "b"], "disambiguator": "a", "leaf_attributes": ["b"],
        "categories": [{"name": "x", "plural": "xs", "label": "Xs", "backbone_filename": "b.json",
                        "base_image": "", "shares_variety_vocab": True, "fan_out": True,
                        "mirror_of": None, "volume_per_kg": "", "pcat_env_var": None}],
        "ambiguous_type_words": ["z"], "default_finishes": ["F"], "fallback_color": "N",
        "last_resort_finishes": {"x": "F"}, "last_resort_quality": "A", "block_finish": "F",
        "dimension_ranges": {"x": {"weight": [0.1, 0.3]}},
        "dimension_defaults": {"x": {"length": 1.0, "height": 1.0, "thickness": 0.02}},
        "finish_phrases": {"f": "p"}, "finish_phrase_default": "p"})


def _write_pack(dirpath, pack_dict):
    import yaml
    (dirpath / "bad.yaml").write_text(yaml.safe_dump(pack_dict), encoding="utf-8")


def test_malformed_category_fails_loud_at_load(tmp_path, monkeypatch):
    # a category dict missing a key must fail LOUD at load (naming the pack), not KeyError deep in settings.
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    del pack["categories"][0]["name"]
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match="missing keys"):
        domain.load_pack("bad")


def test_malformed_range_fails_loud_at_load(tmp_path, monkeypatch):
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    pack["dimension_ranges"]["x"]["weight"] = [0.1]           # not a [lo, hi] pair
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match="lo, hi"):
        domain.load_pack("bad")


_TOY_APPAREL_PACK = """
name: apparel
attributes: [material, color, size]
disambiguator: material
leaf_attributes: [color, size]
categories:
  - {name: shirt, plural: shirts, label: Shirts, backbone_filename: backbone_shirts.json,
     base_image: "", shares_variety_vocab: true, fan_out: true, mirror_of: null, volume_per_kg: "", pcat_env_var: null}
  - {name: pants, plural: pants, label: Pants, backbone_filename: backbone_pants.json,
     base_image: "", shares_variety_vocab: true, fan_out: true, mirror_of: null, volume_per_kg: "", pcat_env_var: null}
ambiguous_type_words: [blend]
default_finishes: [Standard]
fallback_color: Unspecified
last_resort_finishes: {shirt: Standard, pants: Standard}
last_resort_quality: Standard
block_finish: Standard
dimension_ranges:
  shirt: {weight: [0.1, 0.3], length: [0.5, 0.8], width: [0.4, 0.6], height: [0.01, 0.02]}
  pants: {weight: [0.2, 0.5], length: [0.9, 1.2], width: [0.3, 0.5], height: [0.01, 0.02]}
dimension_defaults:
  shirt: {length: 0.7, height: 0.015, thickness: 0.5}
  pants: {length: 1.0, height: 0.015, thickness: 0.4}
finish_phrases: {standard: "a standard finish"}
finish_phrase_default: "a standard finish"
"""


def test_a_completely_different_product_pack_loads(tmp_path, monkeypatch):
    # proof of agnosticism: a NON-stone product type (apparel: material/color/size, shirts/pants) loads
    # through the SAME pack mechanism with no stone assumptions -- this is what "spin it up for a different
    # product type" means (select it with BLOKPORT_DOMAIN_PACK at startup).
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    (tmp_path / "apparel.yaml").write_text(_TOY_APPAREL_PACK, encoding="utf-8")
    p = domain.load_pack("apparel")
    assert p.attributes == ("material", "color", "size")      # a different attribute set
    assert p.disambiguator == "material"
    assert p.leaf_attributes == ("color", "size")
    assert [c["name"] for c in p.categories] == ["shirt", "pants"]   # a different category model
    assert "crystal" not in p.ambiguous_type_words             # no stone vocabulary leaked in
