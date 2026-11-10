"""Products link to their one standardised photo when a published one exists (one good image per listing, user
2026-11-01): its showroom image first (the catalog thumbnail), then its white, side and close-up views; a product with no
standardised photo keeps the images it had. Only each listing's chosen photo is published."""
from __future__ import annotations

import boto3
import pytest

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io import imagestore
from stone_pipeline.stages import images

A, B, C_ = "a" * 64, "b" * 64, "c" * 64
BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"


def _improved(sha, src="sentas"):
    return f"{BASE}/improved/{src}/{sha}.png"


def _shown(urls, views):
    return images.standardised(urls, views, images.photo_lead(urls, views))


def _std(view, sha, src="sentas"):
    return f"{BASE}/standard/{view}/{src}/{sha}.jpg"


def test_the_standard_key_carries_the_sha_like_every_image_key():
    key = imagestore.standard_key("sentas", A, "main")
    assert key == f"{ENV_SEGMENT}/products/standard/main/sentas/{A}.jpg"
    assert imagestore.sha_from_url(key) == A


def test_a_product_shows_one_photo_in_all_its_views_even_when_more_are_published():
    views = {"main": {A, B}, "white": {A, B}, "studio": {A, B}, "close": {A, B}}     # B: chosen for another listing
    out = _shown([_improved(A), _improved(B)], views)
    assert out == [_std("main", A), _std("white", A), _std("studio", A), _std("close", A)]


def test_an_unstandardised_photo_is_dropped_when_others_are_standardised():
    views = {"main": {B}, "white": {B}, "studio": set(), "close": set()}
    out = _shown([_improved(A), _improved(B)], views)
    assert out == [_std("main", B), _std("white", B)]


def test_a_product_with_no_standardised_photo_keeps_its_images():
    urls = [_improved(A), _improved(C_)]
    assert _shown(urls, {"main": {B}, "white": set(), "studio": set(), "close": set()}) == urls


def test_a_close_up_lead_shows_the_views_it_has():
    views = {"main": {A, B}, "white": {A}, "studio": set(), "close": set()}
    out = _shown([_improved(A, "zucchi"), _improved(B, "zucchi")], views)
    assert out == [_std("main", A, "zucchi"), _std("white", A, "zucchi")]


def test_only_the_chosen_photos_are_published_each_with_the_views_its_run_made():
    from stone_pipeline.photos import publish as pub
    run, cleaned = f"{ENV_SEGMENT}/standardise-test/full/sentas/", f"{ENV_SEGMENT}/standardise-test/full-clean/sentas/"
    made = {f"{run}{A}.main.jpg", f"{run}{A}.white.jpg", f"{run}{A}.studio.jpg", f"{run}{A}.close.jpg", f"{run}{A}.slab.png",
            f"{run}{B}.main.jpg", f"{run}{B}.white.jpg", f"{cleaned}{C_}.main.jpg"}                 # B: judged, not chosen
    chosen = [{"source": "sentas", "sha": A, "prefix": run}, {"source": "sentas", "sha": C_, "prefix": cleaned}]
    assert pub.copies(chosen, made) == sorted(
        [(f"{run}{A}.{v}.jpg", imagestore.standard_key("sentas", A, v)) for v in imagestore.STANDARD_VIEWS]
        + [(f"{cleaned}{C_}.main.jpg", imagestore.standard_key("sentas", C_, "main"))])


def test_a_published_view_no_longer_chosen_is_removed():
    from stone_pipeline.photos import publish as pub
    keep, old = imagestore.standard_key("sentas", A, "main"), imagestore.standard_key("sentas", B, "main")
    assert pub.stale([keep, old], [keep]) == [old]


class _Bucket:
    def __init__(self, keys):
        self.keys = keys

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {"KeyCount": sum(k.startswith(Prefix) for k in self.keys)}


def test_a_photo_already_finished_or_held_is_not_worked_on_again():
    from stone_pipeline.photos import source as src
    out = "dev/standardise-test/full/sentas/"
    bucket = _Bucket([f"{out}{A}.main.jpg", f"{out}held/{B}.json"])
    assert src.finished(bucket, out, A) and src.finished(bucket, out, B) and not src.finished(bucket, out, C_)


def test_a_photo_already_retouched_or_refused_is_not_retouched_again():
    from stone_pipeline.photos import retouch_job as rt
    out = "dev/standardise-test/full/sentas/"
    bucket = _Bucket([f"{out}retouched/{A}.jpg", f"{out}retouch_held/{B}.json"])
    assert rt.retouched(bucket, out, A) and rt.retouched(bucket, out, B) and not rt.retouched(bucket, out, C_)


def _retouch_job(monkeypatch, keys, sheet):
    """stone_pipeline.photos.retouch_job.main over one slice of sentas whose bucket holds `keys`: the photos it searched for
    marks, None when it searched none (no model was loaded)."""
    from stone_pipeline.photos import retouch_job as rt
    out = rt.run_prefix("sentas")
    monkeypatch.setenv("SRC", "sentas")
    monkeypatch.delenv("MARKS_SURVEY", raising=False)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _Bucket([f"{out}{k}" for k in keys]))
    monkeypatch.setattr(rt, "sheet_photos", lambda client: {})
    monkeypatch.setattr(rt, "_photo_keys", lambda client, src, photos: [f"dev/products/scraped/sentas/{s}.jpg" for s in sheet])
    searched = []
    monkeypatch.setattr(rt, "_find", lambda client, keys, o: searched.append([imagestore.sha_from_url(k) for k in keys]) or ({}, set()))
    monkeypatch.setattr(rt.marks, "load_sam", lambda: pytest.fail("a model was loaded for a slice with nothing to search"))
    assert rt.main() == 0
    return searched[0] if searched else None


def test_a_recheck_of_a_photo_the_retouch_already_handled_submits_no_retouch_work(monkeypatch):
    """A whole slab drawn under the older outline rule is not finished (the GPU rechecks it), yet its input is fixed: the
    retouched copy its result was made from, the refusal, or, with no record (nothing found on it, or made before the
    retouch existed), the photo its run started from. Never searched again, and no model is loaded for nothing."""
    whole = lambda sha: [f"{sha}.main.jpg", f"{sha}.slab.png"]
    stored = whole(A) + [f"retouched/{A}.jpg"] + whole(B) + whole(C_)
    assert _retouch_job(monkeypatch, stored, [A, B, C_]) is None


def test_a_photo_without_a_result_is_still_searched_for_marks(monkeypatch):
    D = "d" * 64
    assert _retouch_job(monkeypatch, [f"{A}.main.jpg", f"{A}.slab.png"], [A, D]) == [D]


def test_an_excluded_photo_is_never_published_whatever_chose_it():
    from stone_pipeline.photos import publish as pub
    run = f"{ENV_SEGMENT}/standardise-test/full/sentas/"
    made = {f"{run}{A}.main.jpg", f"{run}{A}.white.jpg", f"{run}{B}.main.jpg"}
    chosen = [{"source": "sentas", "sha": A, "prefix": run}, {"source": "sentas", "sha": B, "prefix": run}]
    assert pub.copies(chosen, made, exclude={B}) == sorted(
        (f"{run}{A}.{v}.jpg", imagestore.standard_key("sentas", A, v)) for v in ("main", "white"))


def test_the_exclude_list_reads_one_sha_per_line_with_comments():
    from stone_pipeline.photos import publish as pub
    text = f"# a cut-out that took in the floor\n{A}  # sentas, open book over the wall slabs\n\n"
    assert pub.read_exclude(text) == {A}


def test_a_whole_slab_leads_even_when_a_close_up_comes_first():
    views = {"main": {A, B}, "white": {A, B}, "studio": {B}, "close": {B}}     # A is a close-up: no side view
    out = _shown([_improved(A), _improved(B)], views)
    assert out == [_std("main", B), _std("white", B), _std("studio", B), _std("close", B)]


def test_a_lead_block_is_shown_once():
    views = {"main": {A}, "white": set(), "studio": set(), "close": set()}
    assert _shown([_improved(A)], views) == [_std("main", A)]


def test_a_marks_survey_notes_whether_a_photo_carries_lamp_reflections():
    import numpy as np
    from stone_pipeline.photos import retouch_job as rt
    none = np.zeros((40, 60), np.uint8)
    lamp = none.copy(); lamp[10:14, 20:24] = 1
    assert rt.survey_note(None) == {"marks": False, "flares": False}
    assert rt.survey_note(tuple(rt._pack(m) for m in (none, lamp, none, none, none, none))) == {"marks": True, "flares": True}
    assert rt.survey_note(tuple(rt._pack(m) for m in (lamp, none, none, none, none, none))) == {"marks": True, "flares": False}


def test_a_run_can_be_limited_to_a_list_of_photos():
    from stone_pipeline.photos import source as src
    keys = [f"dev/products/scraped/sentas/{s}.jpg" for s in (A, B, C_)]
    assert src.only(keys, None) == keys                                  # no list: every photo
    assert src.only(keys, [C_, A]) == [keys[0], keys[2]]                 # the listed ones, in the run's own order


def test_a_run_reads_its_environment_s_current_categories_before_its_product_sheet_and_photo_list(monkeypatch):
    from stone_pipeline.io import medusa_inputs as fetch_inputs
    from stone_pipeline.photos import source as src
    from stone_pipeline.config import categories, settings
    calls = []
    monkeypatch.setattr(fetch_inputs, "fetch_attributes", lambda client: calls.append("fetch") or True)
    monkeypatch.setattr(categories, "refresh_category_pcats", lambda: calls.append("refresh"))
    monkeypatch.setattr(src, "_get", lambda client, key: calls.append(key) or
                        (b"[]" if key.endswith(src.C.DEFAULTED_FILE) else b"{}"))
    monkeypatch.setattr(src.index, "build", lambda text, photos, defaulted: calls.append("index") or {})
    assert src.sheet_photos(object()) == {}
    assert calls == ["fetch", "refresh", f"{settings.ENV_SEGMENT}/{src.C.SHEET_KEY}",        # prod has no committed export
                     f"{settings.ENV_SEGMENT}/{src.C.PHOTOS_KEY}", f"{settings.ENV_SEGMENT}/{src.C.DEFAULTED_KEY}", "index"]


def _slab_category(monkeypatch, pcat="pcat_S"):
    from dataclasses import replace
    from stone_pipeline.config import categories
    slab = replace(categories.category("slab"), pcat_id=pcat)
    monkeypatch.setattr(categories, "_BY_NAME", {**categories._BY_NAME, slab.name: slab})


def test_the_product_index_follows_the_categories_refreshed_from_the_export(monkeypatch):
    from stone_pipeline.standardise import index
    _slab_category(monkeypatch, "pcat_PROD_SLABS")                                        # the id prod's export carries
    sheet = "Product Handle,Product Category 1,Product Length,Product Width\nh,pcat_PROD_SLABS,3.0,1.8\n"
    assert index.build(sheet, {"h": [("sentas", A)]}, frozenset())[A]["format"] == "slab"


def test_listings_take_each_product_s_own_photos_in_its_order_from_the_photo_list(monkeypatch):
    from stone_pipeline.standardise import index
    _slab_category(monkeypatch)
    sheet = ("Product Handle,Variant Sku,STN Variation Id,STN Finish Id,Product Category 1,Product Image 1\n"
             "one,SEN-1,v1,f1,pcat_S,x\ntwo,SEN-2,v1,f2,pcat_S,\nnone,SEN-3,v1,f1,pcat_S,\n")
    photos = {"one": [("sentas", B), ("sentas", A)], "two": [("sentas", A)], "none": []}
    assert index.listings(sheet, photos) == [
        {"key": "one", "sku": "SEN-1", "source": "sentas", "stone": ("v1", "f1"), "format": "slab", "photos": [B, A]},
        {"key": "two", "sku": "SEN-2", "source": "sentas", "stone": ("v1", "f2"), "format": "slab", "photos": [A]}]


def test_a_published_listing_keeps_all_its_own_photos_whatever_the_sheet_shows(monkeypatch):
    # once a listing's photo is published, the emitted sheet carries only that photo's standardised views: the photo
    # list still has every photo of the listing, so a later run can try its further photos (a whole slab after a close-up)
    from stone_pipeline.standardise import index
    _slab_category(monkeypatch)
    sheet = ("Product Handle,Variant Sku,STN Variation Id,STN Finish Id,Product Category 1,Product Length,Product Width,"
             "Product Image 1,Product Image 2\n"
             f"one,SEN-1,v1,f1,pcat_S,3.0,1.8,{_std('main', A)},{_std('white', A)}\n")
    photos = {"one": [("sentas", A), ("sentas", B)]}
    assert index.listings(sheet, photos) == [
        {"key": "one", "sku": "SEN-1", "source": "sentas", "stone": ("v1", "f1"), "format": "slab", "photos": [A, B]}]
    assert set(index.build(sheet, photos, frozenset())) == {A, B}                                      # the GPU job sees both


def test_a_photo_several_listings_show_is_their_stone_s_or_a_conflict():
    # a photo belongs to a stone and finish: slabs of one block share it (siblings); the same photo on another variety or
    # finish (prod: Luminatta Leathered and Polished at zucchi, Royal Black and Toros Black at marenostone) is a conflict
    from stone_pipeline.standardise import index
    D_ = "d" * 64

    def l(key, stone, *photos, source="sentas"):
        return {"key": key, "sku": key.upper(), "source": source, "stone": stone, "format": "slab", "photos": list(photos)}
    listings = [l("block-1", ("v1", "f1"), A, B), l("block-2", ("v1", "f1"), A), l("leathered", ("v2", "lea"), D_),
                l("polished", ("v2", "pol"), D_, B), l("alone", ("v3", "f1"), "e" * 64), l("other-src", ("v9", "f9"), A, source="zucchi")]
    shared = index.shared_photos(listings)
    assert shared == {("sentas", A): index.Shared("siblings", ("block-1", "block-2")),
                      ("sentas", B): index.Shared("conflict", ("block-1", "polished")),
                      ("sentas", D_): index.Shared("conflict", ("leathered", "polished"))}
