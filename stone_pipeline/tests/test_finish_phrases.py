"""finish_phrases: the per-finish sentence the description template appends ("... it offers {phrase}.").
Scraper-side copy only -- the finish VOCABULARY comes from Medusa. Every finish that occurs on a real
product must carry a finish-true bespoke phrase, never the generic default.
"""
from stone_pipeline.config.domain import load_pack


def test_every_finish_sold_by_the_vendors_has_a_bespoke_phrase():
    p = load_pack("stone")
    sold = ["Polished", "Leathered", "Honed", "Raw", "Brushed", "Flamed", "Textured"]   # observed on emitted products
    for finish in sold:
        phrase = p.finish_phrases.get(finish.lower())      # derive looks up finish_name.lower()
        assert phrase and phrase != p.finish_phrase_default, finish


def test_raw_and_textured_phrases_are_finish_true():
    p = load_pack("stone")
    assert "quarry-fresh" in p.finish_phrases["raw"] and "unworked" in p.finish_phrases["raw"]
    assert "tactile" in p.finish_phrases["textured"]
