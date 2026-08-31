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
                        "mirror_of": None, "volume_per_kg": "", "pcat_env_var": None, "default_form": True}],
        "ambiguous_type_words": ["z"], "generic_descriptors": ["g"], "generic_material_word": "m",
        "default_finishes": ["F"], "fallback_color": "N",
        "last_resort_finishes": {"x": "F"}, "last_resort_quality": "A", "block_finish": "F",
        "in_stock_fallback_qty": {"x": 5},
        "dimension_ranges": {"x": {"weight": [0.1, 0.3]}},
        "dimension_defaults": {"x": {"length": 1.0, "height": 1.0, "thickness": 0.02}},
        "finish_phrases": {"f": "p"}, "finish_phrase_default": "p", "default_density": 700})


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


def test_category_missing_from_a_per_category_map_fails_loud(tmp_path, monkeypatch):
    # V1 cross-check: a category with no entry in a per-category map (the wood-onboarding footgun -- e.g.
    # 'board' declared but absent from dimension_ranges) must fail LOUD at load, not KeyError in derive.
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    del pack["dimension_ranges"]["x"]                          # category 'x' now has no range entry
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match="dimension_ranges is missing an entry for categories"):
        domain.load_pack("bad")


def test_map_key_not_a_declared_category_fails_loud(tmp_path, monkeypatch):
    # V1 cross-check (reverse): a per-category map key that is not a declared category is also a pack bug.
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    pack["in_stock_fallback_qty"]["ghost"] = 3                 # 'ghost' is not a category
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match="not in the declared categories"):
        domain.load_pack("bad")


def test_leaf_attribute_not_in_attributes_fails_loud(tmp_path, monkeypatch):
    pack = _valid_pack_dict()
    pack["leaf_attributes"] = ["b", "ghost"]                   # 'ghost' not in attributes [a,b]
    _write_pack(tmp_path, pack)
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    domain.active_pack.cache_clear()
    with pytest.raises(ValueError, match="leaf_attributes"):
        domain.load_pack("bad")


def test_disambiguator_inside_leaf_attributes_fails_loud(tmp_path, monkeypatch):
    pack = _valid_pack_dict()
    pack["leaf_attributes"] = ["a", "b"]                       # 'a' is the disambiguator -> must not be a leaf
    _write_pack(tmp_path, pack)
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    domain.active_pack.cache_clear()
    with pytest.raises(ValueError, match="disambiguator"):
        domain.load_pack("bad")


def test_mirror_of_unknown_category_fails_loud(tmp_path, monkeypatch):
    pack = _valid_pack_dict()
    pack["categories"][0]["mirror_of"] = "ghost"              # names no declared category
    _write_pack(tmp_path, pack)
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    domain.active_pack.cache_clear()
    with pytest.raises(ValueError, match="mirror_of"):
        domain.load_pack("bad")


def test_disambiguator_outside_attributes_fails_loud(tmp_path, monkeypatch):
    # V2 cross-check: the identity attribute must be part of the attribute vocabulary, else the Key is built
    # from an attribute the pipeline never resolves.
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    pack["disambiguator"] = "not_an_attribute"
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match="disambiguator .* is not in attributes"):
        domain.load_pack("bad")


def test_no_default_form_fails_loud(tmp_path, monkeypatch):
    # V3: the pipeline always needs a fallback form; a pack that declares none must fail LOUD at load.
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    del pack["categories"][0]["default_form"]                 # now zero default_form categories
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match="exactly one category must set default_form"):
        domain.load_pack("bad")


def test_two_bulk_forms_fails_loud(tmp_path, monkeypatch):
    # V3: the bulk/solid form is singular; two is a pack bug (which one drives is_block?).
    monkeypatch.setattr(domain, "_DOMAINS_DIR", tmp_path)
    pack = _valid_pack_dict()
    pack["categories"].append({**pack["categories"][0], "name": "y", "default_form": False, "bulk_form": True})
    pack["categories"][0]["bulk_form"] = True                 # two bulk_form categories now
    pack["in_stock_fallback_qty"]["y"] = 1                    # keep V1 (per-category maps) satisfied
    pack["dimension_ranges"]["y"] = {"weight": [0.1, 0.3]}
    pack["dimension_defaults"]["y"] = {"length": 1.0, "height": 1.0, "thickness": 0.02}
    _write_pack(tmp_path, pack)
    with pytest.raises(ValueError, match="at most one category may set bulk_form"):
        domain.load_pack("bad")


def test_stone_declares_the_category_roles():
    # the stone pack's roles: slab is the default/fallback form, block is the uncut/bulk form (drives is_block),
    # tile mirrors slab. This is what makes the historical slab/block/tile behaviour pack-driven, not hardcoded.
    from stone_pipeline.config import settings
    assert settings.default_form_name() == "slab"
    assert settings.bulk_form_name() == "block"
    assert settings.category("tile").mirror_of == "slab"


def test_name_heuristics_are_pack_driven(monkeypatch):
    # GAP F: the granite-code keep-exception and the trailing lone-letter grade strip/flag are STONE corpus
    # rules, now pack-declared. Under stone they fire (byte-identical); a domain that declares neither leaves
    # names intact instead of mangling them (the wood footgun).
    import dataclasses
    from stone_pipeline.core import text
    # stone pack: grade letter collapses to the base variety; granite 'G682' survives as a real name
    assert text.clean_variety_name("Rosal C") == "Rosal"
    assert text.clean_variety_name("G682 Kashmir") == "G682 Kashmir"
    assert text.looks_code_shaped("Trani Bianco H") == "lone_letter"
    # a domain that grades by neither: the trailing letter is kept, granite 'G682' is treated as a code
    plain = dataclasses.replace(domain.active_pack(), trailing_grade_letters=False, name_code_pattern=None)
    monkeypatch.setattr(domain, "active_pack", lambda: plain)
    assert text.clean_variety_name("Rosal C") == "Rosal C"       # trailing letter NOT a grade -> kept
    assert text.clean_variety_name("G682 Kashmir") == "Kashmir"  # no real-code pattern -> stripped as a code
    assert text.looks_code_shaped("Trani Bianco H") == ""        # not flagged as a grade


def test_texture_colour_classification_is_pack_gated(monkeypatch):
    # GAP B: stone classifies a variety's colour from its product image; a domain can opt out
    # (classify_texture_color=false), and then the classify() palette need not exist in Medusa.
    import dataclasses
    from types import SimpleNamespace
    from stone_pipeline.reference import loaders
    from stone_pipeline.stages.variety_color import CLASSIFIABLE_COLORS
    pack = domain.active_pack()
    assert pack.classify_texture_color is True
    # a fake Medusa vocab that has the pack defaults but is MISSING the classify palette colours
    def resolve_id(vocab, val):
        if vocab == "color" and val in CLASSIFIABLE_COLORS:
            return None                          # palette colour absent from Medusa
        return (vocab, "id")                     # every pack-default value resolves
    ref = SimpleNamespace(attributes=SimpleNamespace(resolve_id=resolve_id))
    monkeypatch.setattr(loaders, "load_synonyms", lambda vocab: {})
    with pytest.raises(ValueError, match="absent from attributes.csv"):
        loaders._assert_pack_defaults_resolve(ref)          # classify ON -> palette required -> fails loud
    off = dataclasses.replace(pack, classify_texture_color=False)
    monkeypatch.setattr(domain, "active_pack", lambda: off)
    loaders._assert_pack_defaults_resolve(ref)              # classify OFF -> palette not required -> passes


_TOY_APPAREL_PACK = """
name: apparel
attributes: [material, color, size]
disambiguator: material
leaf_attributes: [color, size]
categories:
  - {name: shirt, plural: shirts, label: Shirts, backbone_filename: backbone_shirts.json,
     base_image: "", shares_variety_vocab: true, fan_out: true, mirror_of: null, volume_per_kg: "", pcat_env_var: null, default_form: true}
  - {name: pants, plural: pants, label: Pants, backbone_filename: backbone_pants.json,
     base_image: "", shares_variety_vocab: true, fan_out: true, mirror_of: null, volume_per_kg: "", pcat_env_var: null}
ambiguous_type_words: [blend]
generic_descriptors: [garment, the]
generic_material_word: garment
default_finishes: [Standard]
fallback_color: Unspecified
last_resort_finishes: {shirt: Standard, pants: Standard}
last_resort_quality: Standard
block_finish: Standard
default_density: 300
in_stock_fallback_qty: {shirt: 10, pants: 10}
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
    assert p.default_density == 300.0                          # material density is a pack field, not hardcoded 2700
