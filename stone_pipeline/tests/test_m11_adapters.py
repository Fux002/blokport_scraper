"""M11 DoD: each adapter's fixture passes; marenostone runs and routes its
generic-descriptor rows to the gap queue rather than guessing. Each new source
reuses the spine unchanged (no stage touched).
"""

from __future__ import annotations

import glob

import pytest

from stone_pipeline.adapters import selftest
from stone_pipeline.adapters.base import read_scrape_csv
from stone_pipeline.adapters.tokens import (
    _colour_lookup, _match_colour, explicit_type_word, extract_color, known_values, strip_variety)
from stone_pipeline.config.settings import SETTINGS
from stone_pipeline.run import run_source


@pytest.mark.parametrize("source", sorted(selftest.REGISTRY))  # every auto-registered adapter
def test_fixture_selftest_passes(source):
    ok, message = selftest.run_fixture(source)
    assert ok, message


def test_every_adapter_has_a_fixture_and_config():
    # a source is fully onboarded only when its adapter, fixture, and sources.yaml agree;
    # this catches a new adapter that forgot one of the wiring steps.
    from stone_pipeline.config.sources import load_sources
    configured = set(load_sources())
    for source in selftest.REGISTRY:
        assert (selftest.fixture_dir(source) / "input.csv").exists(), f"{source}: missing fixture"
        assert source in configured, f"{source}: missing from sources.yaml"


def test_generic_descriptor_yields_empty_variety():
    # a pure colour+type+format descriptor has no named variety
    assert strip_variety("Cream Marble Tile") == ""
    assert strip_variety("Black Granite Slab") == ""
    # a real variety token survives
    assert strip_variety("Pietra Gray Marble Slab") == "Pietra"


def test_match_colour_is_position_aware_and_boundary_safe():
    # _match_colour is PURE: inject the vocabulary and assert the logic alone (no reference I/O). It is
    # position-aware (first colour by TEXT order) and boundary-safe (whole words, via the match key).
    lookup = {"black": "Black", "white": "White", "grey": "Grey", "blue": "Blue", "gold": "Gold",
              "golden": "Golden", "cinza": "Grey", "preto": "Black", "rosa": "Rose",
              "anthracite": "Anthracite"}
    cases = {
        "CIN Marmore Cinza Blue Sky": "Grey",      # position: the structural colour, not trade-name Blue
        "CIN Granito Preto Rio Negro": "Black",     # synonym recognised
        "Black and White": "Black",                 # first by position
        "Rosá": "Rose",                             # accent folded by match_key
        "Anthracite Grey X": "Anthracite",          # a value present only in the injected vocab
        "Golden": "Golden",                         # boundary: 'Golden' is not 'Gold'
        "Alaska Gold": "Gold",
        "Mandrak": "",                              # no colour word
        "": "",
    }
    for text, expected in cases.items():
        assert _match_colour(text, lookup) == expected, text


def test_extract_color_reads_live_vocab_and_synonyms():
    # extract_color recognises against the live vocab (committed attributes.csv) + synonyms (color.csv):
    # English canonical AND Portuguese synonyms resolve, position-aware over a trade-name colour.
    assert extract_color("Alaska Gold") == "Gold"
    assert extract_color("Acadian Night is a black granite") == "Black"
    assert extract_color("CIN Granito Preto Rio Negro 2cm") == "Black"     # preto -> Black (synonym)
    assert extract_color("CIN Marmore Cinza Blue Sky 2cm") == "Grey"       # position, not Blue
    assert extract_color("CIN Granito Marrom Absoluto 2cm") == "Brown"     # marrom -> Brown (new synonym)
    assert extract_color("No colour here") == ""


def test_colour_lookup_merges_canonical_and_synonyms():
    lookup = _colour_lookup()
    assert lookup["black"] == "Black"          # canonical (from attributes.csv)
    assert lookup["preto"] == "Black"          # synonym (from color.csv)
    assert lookup["gray"] == "Grey"            # synonym; canonical spelling is 'Grey'


def test_known_values_sources_from_the_medusa_export():
    # the ONE canonical vocabulary is the live export, shared with the resolver -- not a hardcoded list.
    colours, types = known_values("color"), known_values("type")
    assert "Black" in colours and "White" in colours and len(colours) > 10
    assert "Granite" in types and "Quartzite" in types


def test_zucchi_color_prefers_the_structured_portuguese_name():
    # the colour lives in product_name_pt; the English trade name and description are secondary. A row
    # whose English name conflicts (Wonderful Black) takes the authoritative PT colour (Cinza -> Grey).
    from stone_pipeline.adapters.zucchi import _color
    assert _color({"product_name_pt": "CIN Quartzito Verde Acquamare 2cm",
                   "product_name_en": "Acquamare"}) == "Green"
    assert _color({"product_name_pt": "CIN Marmore Cinza Blue Sky 2cm",
                   "product_name_en": "Wonderful Black"}) == "Grey"          # PT wins the conflict
    assert _color({"product_name_pt": "", "product_name_en": "Alaska Gold"}) == "Gold"   # EN fallback
    assert _color({"product_name_pt": "", "product_name_en": "Mandrak",
                   "description": ""}) == ""


def test_explicit_type_word_uses_live_type_vocab_with_guards():
    # type-word detection sources its vocabulary from the live export + synonyms (folded TYPE_SYNONYMS),
    # while the detection guards (ambiguous words, negation) are unchanged.
    assert explicit_type_word("Azul White Quartzite") == "Quartzite"   # trailing type word
    assert explicit_type_word("Sodalite Baia") == "Sodalite"           # leading synonym (now in type.csv)
    assert explicit_type_word("Spectra Crystal") is None               # descriptor-prone -> guarded
    assert explicit_type_word("Falsa Agata") is None                   # negation guard
    assert explicit_type_word("Acadian Night") is None                 # no type word


def test_recognize_type_reads_a_type_field_against_live_vocab():
    from stone_pipeline.adapters.tokens import recognize_type
    # a real Medusa type (canonical or synonym) is recognised; a classification tag is not
    assert recognize_type("Granite") == "Granite"
    assert recognize_type("quartzite") == "Quartzite"
    assert recognize_type("Marble") == "Marble"
    assert recognize_type("Exotic") == ""          # varsha classification tag, not a type
    assert recognize_type("Stock Clearance") == ""
    assert recognize_type("Rough Blocks") == ""
    assert recognize_type("") == ""


def test_varsha_reads_stone_type_from_composition_head_only_when_real():
    adapter = selftest.REGISTRY["varsha"]
    real = adapter.adapt_record({"bundle_id": "1", "material_name": "BLACK ABSOLUTE",
                                 "composition": "Granite / ", "finish": "Polished", "quality": "Premium"})
    tag = adapter.adapt_record({"bundle_id": "2", "material_name": "ALPINUS",
                                "composition": "EXOTIC / ", "finish": "Polished", "quality": "Premium"})
    assert real.raw_type == "Granite"   # real type recovered from composition (was previously discarded)
    assert tag.raw_type == ""           # a classification tag leaves type empty (held / matched, not tag)


def test_marenostone_routes_generic_to_gaps_not_guesses(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    manifest = run_source("marenostone", outputs_dir=out, state_dir=out)
    # generic descriptors must not be guessed into output: they gap
    assert manifest.gap_kind_counts.get("GapKind.missing_variation", 0) > 0
    # the spine still emits the rows that DO resolve fully
    assert manifest.totals["emitted"] >= 1
    # no emitted row lacks a variation id (no guess reached output)
    import csv
    path = glob.glob(str(out / "**" / "medusa_import.csv"), recursive=True)[0]
    for row in csv.DictReader(open(path, encoding="utf-8-sig")):
        assert row["STN Variation Id"].strip()


def test_blank_sku_mints_not_drops(tmp_path):
    # marenostone ships blank SKUs; they must mint a surrogate, never drop
    frame = read_scrape_csv(
        SETTINGS.paths.tests_fixtures_dir / "marenostone_products_20260601_155229.csv"
    )
    rows = selftest.REGISTRY["marenostone"].adapt(frame)
    # adapter keeps every row (blank keys included); minting happens in Stage 2
    assert len(rows) == frame.height


def test_codes_auto_detected_from_corpus_no_hardcoding():
    # supplier codes are DISCOVERED from how they fan out across a source's names — no string
    # is hardcoded. varsha's 'Z'/'ZB' front many varieties, so they are detected and stripped;
    # a real shared prefix that does not fan out, and single z-words, are kept.
    from stone_pipeline.core.text import detect_code_prefixes, clean_variety_name
    corpus = ["Z ASTORIA", "Z AQUA BLUE", "Z B FUSION BLACK", "ZB PATAGONIA", "ZB AZUL",
              "Z MONALISA", "Carrara", "Statuario", "Mt Blanc", "ZEBRINO"]
    codes = detect_code_prefixes(corpus)
    assert "z" in codes and "zb" in codes
    c = lambda s: clean_variety_name(s, (), codes)
    assert c("Z ASTORIA") == "ASTORIA"
    assert c("Z B FUSION BLACK") == "FUSION BLACK"
    assert c("ZB PATAGONIA") == "PATAGONIA"        # collapsed code, auto-detected
    assert c("Z BLACK FOREST") == "BLACK FOREST"   # embedded B kept (lone-letter run stops)
    assert c("ZEBRINO") == "ZEBRINO"               # single z-word, no fan-out -> kept
    assert c("Carrara") == "Carrara" and c("Mt Blanc") == "Mt Blanc"  # real names untouched


def test_clean_variety_name_generic_and_prefix():
    from stone_pipeline.core.text import clean_variety_name as c
    # lone-letter leading codes + stray punctuation
    assert c("A Bianco") == "Bianco"
    assert c("  - Statuario ") == "Statuario"
    # ANY number-bearing token is stripped ('No.' too) — no stone uses numbers in its name...
    assert c("Super – 1.08") == "Super" and c("Wave - 1.06") == "Wave"
    assert c("Marjan – No. 426") == "Marjan"
    assert c("883 Black") == "Black"
    assert c("Arizona 3D Grey") == "Arizona Grey" and c("Matrix 3D") == "Matrix"
    assert c("Calacatta 2cm") == "Calacatta"
    # ...EXCEPT granite's 'G' + number code, which IS the real name -> kept
    assert c("G682") == "G682" and c("G032") == "G032"
    assert c("G682 (Sunset Gold)") == "G682 (Sunset Gold)"
    # real names untouched (no false positives)
    assert c("La Perla") == "La Perla" and c("El Dorado") == "El Dorado"
    # source-declared prefix (varsha 'Z'/'ZB') strips on top of the generic rules
    assert c("Z B Fusion Black", (r"^[Zz]\s*[Bb]?\s+",)) == "Fusion Black"
    assert c("ZB Patagonia", (r"^[Zz]\s*[Bb]?\s+",)) == "Patagonia"


def test_accents_folded_consistently_everywhere():
    # a variety name never carries a special character; the SAME folding applies to the display
    # name, the slug/key, and the match key, so 'Porriño' and 'Porrino' are one stone.
    from stone_pipeline.core.text import ascii_fold, title_case, slugify
    from stone_pipeline.matching.projections import norm
    assert ascii_fold("Rosa Porriño") == "Rosa Porrino"
    assert title_case("são GABRIEL") == "Sao Gabriel"
    assert slugify("Cinza Corumbá") == "cinza-corumba"
    assert norm("Porriño") == norm("Porrino")            # accent-insensitive matching
    assert all(ord(c) < 128 for c in title_case("Rosa Porriño"))  # output is pure ASCII


def test_load_frame_makes_non_file_sources_first_class(tmp_path, monkeypatch):
    # a source that overrides load_frame (e.g. an API/DB) runs through the WHOLE pipeline without
    # the CSV ingest being touched; the run id uses the timestamp it hands back.
    import pytest

    from stone_pipeline import adapters as reg
    from stone_pipeline import run as run_mod
    from stone_pipeline.adapters.base import read_scrape_csv
    live = run_mod.find_scrape_file("marenostone")
    if live is None:
        pytest.skip("no marenostone scrape present")
    frame = read_scrape_csv(live)                              # an in-memory frame (could be from an API)
    monkeypatch.setattr(reg.REGISTRY["marenostone"], "load_frame",
                        lambda scrape_path=None: (frame, "20260101_000000", "api://marenostone"))

    def _boom(_source):
        raise AssertionError("CSV ingest was used — load_frame override ignored")
    monkeypatch.setattr(run_mod, "find_scrape_file", _boom)   # fails if the file path is taken
    manifest = run_mod.run_source("marenostone", outputs_dir=tmp_path, state_dir=tmp_path)
    assert manifest.run_id == "marenostone_20260101_000000"   # used the load_frame timestamp token
    assert manifest.totals["rows"] >= 1                        # ran the in-memory frame end to end


def test_looks_code_shaped_flags_codes_not_real_names():
    # supplier codes/grades -> flagged (routed to review, never minted/merged); real short stone
    # names and granite G-codes -> NOT flagged (no false positives), colours stay distinct.
    from stone_pipeline.core.text import looks_code_shaped as L
    assert L("Rosal C") == "lone_letter" and L("Trani Bianco H") == "lone_letter"
    assert L("Colonial White - M") == "lone_letter" and L("Bianco V") == "lone_letter"
    assert L("Gs") == "bare_code" and L("Zb") == "bare_code" and L("Lg") == "bare_code"
    assert not L("Ice") and not L("Oak") and not L("Ash") and not L("Sun") and not L("Tan")
    assert not L("G682") and not L("Agata Black") and not L("Carrara") and not L("Cristallo Divine")


def test_emit_consolidate_folds_grade_codes_only():
    # graded variants fold into one base (originals -> aliases); distinct same-name variants are
    # NOT deduped and real names never merge.
    from stone_pipeline.stages.emit_catalog import _consolidate
    rows=[{"Key":"slab_limestone_rosal_b_1","Name":"Rosal B","Image":"","Aliases":"Rosal Cd","Volume per kg (m³/kg)":""},
          {"Key":"slab_limestone_rosal_c_1","Name":"Rosal C","Image":"","Aliases":"","Volume per kg (m³/kg)":""},
          {"Key":"slab_limestone_rosal_original_1","Name":"Rosal Original","Image":"","Aliases":"","Volume per kg (m³/kg)":""},
          {"Key":"slab_agate_agata_black_1","Name":"Agata Black","Image":"","Aliases":"","Volume per kg (m³/kg)":""},
          {"Key":"slab_agate_agata_black_2","Name":"Agata Black","Image":"","Aliases":"","Volume per kg (m³/kg)":""}]
    out=_consolidate(rows)
    names=[r["Name"] for r in out]
    assert "Rosal" in names and "Rosal C" not in names and "Rosal B" not in names   # folded
    assert names.count("Agata Black")==2                                            # NOT deduped
    assert "Rosal Original" in names                                                # distinct, kept
    rosal=next(r for r in out if r["Name"]=="Rosal")
    assert "Rosal B" in rosal["Aliases"] and "Rosal C" in rosal["Aliases"] and "Rosal Cd" in rosal["Aliases"]


def test_variant_image_link_never_points_at_a_missing_image():
    # a variant advertises its S3 image link ONLY when the {Key}.png actually exists -- a
    # product-backed variant whose image was never generated (the marjan_silver bug) stays blank.
    from stone_pipeline.stages.emit_catalog import _image_link
    base = "https://b.s3/dev/variations/"
    s3 = {"block_travertine_marjan_6c92"}  # only this one is actually on S3
    # product-backed but NOT on S3 -> blank (was a 404 before)
    assert _image_link("block_travertine_marjan_silver_4735", False, True, s3, base) == ""
    # on S3 -> stamped
    assert _image_link("block_travertine_marjan_6c92", False, True, s3, base) == f"{base}block_travertine_marjan_6c92.png"
    # already-imaged variant not in the (stale) listing -> still blank when S3 is authoritative
    assert _image_link("k", True, False, s3, base) == ""


def test_variant_image_link_falls_back_when_s3_unreachable():
    # S3 unreachable (s3_keys=None, e.g. CI): keep the prior heuristic so output is unchanged.
    from stone_pipeline.stages.emit_catalog import _image_link
    base = "https://b.s3/dev/variations/"
    assert _image_link("k", False, True, None, base) == f"{base}k.png"   # product-backed -> stamp
    assert _image_link("k", True, False, None, base) == f"{base}k.png"   # already-imaged -> stamp
    assert _image_link("k", False, False, None, base) == ""              # neither -> blank


def test_plural_format_tag_resolves():
    # a scraper that tags its format in the plural ('Slabs'/'Blocks'/'Tiles') must still resolve the
    # explicit tag, not silently fall through to the slab default.
    from stone_pipeline.config.settings import category
    assert category("Slabs").name == "slab"
    assert category("Blocks").name == "block"
    assert category("Tiles").name == "tile"
    assert category("Slab").name == "slab"     # singular still works
    assert category("nonsense") is None


def test_source_codes_are_unique_across_sources():
    # a new source with a duplicate/typo'd source_code would collide SKUs and the delist-scoping
    # prefix -- assert uniqueness so adding a scraper can't silently alias another source.
    from stone_pipeline import adapters as ar
    from stone_pipeline.config.sources import load_source
    seen: dict[str, str] = {}
    for src in ar.REGISTRY:
        code = load_source(src).source_code
        assert code, f"{src} has an empty source_code"
        assert code not in seen, f"source_code {code!r} collides: {src} and {seen[code]}"
        seen[code] = src


def test_zucchi_weight_normalized_to_per_piece():
    # OBS-3: zucchi reports weight_kg_net for the whole BUNDLE; the adapter (the only source-format-aware
    # layer) divides by slab_count so the shared pipeline receives canonical PER-PIECE weight.
    from stone_pipeline.adapters.zucchi import _per_piece_kg
    assert _per_piece_kg({"weight_kg_net": "3000", "slab_count": "6"}) == "500.0"
    assert _per_piece_kg({"weight_kg_net": "3000", "slab_count": "0"}) == ""   # div0 -> blank (derive synthesizes)
    assert _per_piece_kg({"weight_kg_net": "", "slab_count": "6"}) == ""       # missing weight -> blank
