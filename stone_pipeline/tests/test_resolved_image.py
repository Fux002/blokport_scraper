"""Resolved rows must carry a scraper image. They never did -- the row exposed src_url but no image, so
resolved cards depended entirely on a Medusa SKU fallback; when it stopped matching, images vanished. The
parquet stores the image lists as JSON strings, so the extraction must parse them."""

from stone_pipeline.config import resolved


def test_first_image_parses_json_list_prefers_supplier_photo():
    rec = {"raw_image_urls": '["https://x/a.jpg", "https://x/b.jpg"]',
           "image_keys": '["https://s3/k.jpg"]'}
    assert resolved._first_image(rec) == "https://x/a.jpg"       # supplier photo first


def test_first_image_falls_back_to_image_keys():
    rec = {"raw_image_urls": "", "image_keys": '["https://s3/k.jpg"]'}
    assert resolved._first_image(rec) == "https://s3/k.jpg"


def test_first_image_tolerates_empty_and_garbage():
    assert resolved._first_image({}) == ""
    assert resolved._first_image({"raw_image_urls": "[]", "image_keys": "[]"}) == ""
    assert resolved._first_image({"raw_image_urls": "not json"}) == "not json"   # plain URL, not a crash


def test_first_image_skips_empty_entries():
    assert resolved._first_image({"raw_image_urls": '["", "https://x/b.jpg"]'}) == "https://x/b.jpg"


def test_resolved_row_carries_widen_keyed_on_the_decided_target(monkeypatch):
    """Blokport reads `widen` on resolved rows to show "added to documented origins" + prefill the amend
    checkbox. It is keyed on the DECIDED target variety (the bound/minted identity), not the scraped name."""
    rec = {"src_site": "zucchi", "surrogate_key": "1", "raw_name": "Amazon Marble",
           "variety_match_key": "Amazon Marble", "variation_key": "k", "variation_name": "Amazon White",
           "variation_method": "", "type_name": "Marble", "type_method": "", "color_name": "",
           "origin_country_code": "", "origin_source": "", "src_url": "", "raw_image_urls": "", "image_keys": ""}
    scoped = {("zucchi", "amazon marble"): ("Silver Stream", "Marble")}   # bound to Silver Stream
    vorigins = {("silver stream", "marble"): "IR"}                        # widened
    row = resolved._row(rec, scoped, {}, {}, vorigins)
    assert row["decision"]["name"] == "Silver Stream"
    assert row["widen"] is True and row["documented_origin"] == "IR"
    # not widened -> False
    row2 = resolved._row(rec, scoped, {}, {}, {})
    assert row2["widen"] is False and row2["documented_origin"] is None
