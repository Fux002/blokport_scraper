"""Products link to their one standardised photo when a published one exists (one good image per listing, user
2026-11-01): its showroom image first (the catalog thumbnail), then its white, side and close-up views; a product with no
standardised photo keeps the images it had. Only each listing's chosen photo is published."""
from __future__ import annotations

from stone_pipeline.config.settings import ENV_SEGMENT
from stone_pipeline.io import imagestore
from stone_pipeline.stages import images

A, B, C_ = "a" * 64, "b" * 64, "c" * 64
BASE = f"https://bucket.s3.amazonaws.com/{ENV_SEGMENT}/products"


def _improved(sha, src="sentas"):
    return f"{BASE}/improved/{src}/{sha}.png"


def _std(view, sha, src="sentas"):
    return f"{BASE}/standard/{view}/{src}/{sha}.jpg"


def test_the_standard_key_carries_the_sha_like_every_image_key():
    key = imagestore.standard_key("sentas", A, "main")
    assert key == f"{ENV_SEGMENT}/products/standard/main/sentas/{A}.jpg"
    assert imagestore.sha_from_url(key) == A


def test_a_product_shows_one_photo_in_all_its_views_even_when_more_are_published():
    views = {"main": {A, B}, "white": {A, B}, "studio": {A, B}, "close": {A, B}}     # B: chosen for another listing
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", A), _std("white", A), _std("studio", A), _std("close", A)]


def test_an_unstandardised_photo_is_dropped_when_others_are_standardised():
    views = {"main": {B}, "white": {B}, "studio": set(), "close": set()}
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", B), _std("white", B)]


def test_a_product_with_no_standardised_photo_keeps_its_images():
    urls = [_improved(A), _improved(C_)]
    assert images.standardised(urls, {"main": {B}, "white": set(), "studio": set(), "close": set()}) == urls


def test_a_close_up_lead_shows_the_views_it_has():
    views = {"main": {A, B}, "white": {A}, "studio": set(), "close": set()}
    out = images.standardised([_improved(A, "zucchi"), _improved(B, "zucchi")], views)
    assert out == [_std("main", A, "zucchi"), _std("white", A, "zucchi")]


def test_only_the_chosen_photos_are_published_each_with_the_views_its_run_made():
    from deploy import standardise_publish as pub
    run, cleaned = f"{ENV_SEGMENT}/standardise-test/full/sentas/", f"{ENV_SEGMENT}/standardise-test/full-clean/sentas/"
    made = {f"{run}{A}.main.jpg", f"{run}{A}.white.jpg", f"{run}{A}.studio.jpg", f"{run}{A}.close.jpg", f"{run}{A}.slab.png",
            f"{run}{B}.main.jpg", f"{run}{B}.white.jpg", f"{cleaned}{C_}.main.jpg"}                 # B: judged, not chosen
    chosen = [{"source": "sentas", "sha": A, "prefix": run}, {"source": "sentas", "sha": C_, "prefix": cleaned}]
    assert pub.copies(chosen, made) == sorted(
        [(f"{run}{A}.{v}.jpg", imagestore.standard_key("sentas", A, v)) for v in imagestore.STANDARD_VIEWS]
        + [(f"{cleaned}{C_}.main.jpg", imagestore.standard_key("sentas", C_, "main"))])


def test_a_published_view_no_longer_chosen_is_removed():
    from deploy import standardise_publish as pub
    keep, old = imagestore.standard_key("sentas", A, "main"), imagestore.standard_key("sentas", B, "main")
    assert pub.stale([keep, old], [("run/a.main.jpg", keep)]) == [old]


class _Bucket:
    def __init__(self, keys):
        self.keys = keys

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        return {"KeyCount": sum(k.startswith(Prefix) for k in self.keys)}


def test_a_photo_already_finished_or_held_is_not_worked_on_again():
    from deploy import standardise_source as src
    out = "dev/standardise-test/full/sentas/"
    bucket = _Bucket([f"{out}{A}.main.jpg", f"{out}held/{B}.json"])
    assert src.finished(bucket, out, A) and src.finished(bucket, out, B) and not src.finished(bucket, out, C_)


def test_a_photo_already_retouched_or_refused_is_not_retouched_again():
    from deploy import standardise_retouch as rt
    out = "dev/standardise-test/full/sentas/"
    bucket = _Bucket([f"{out}retouched/{A}.jpg", f"{out}retouch_held/{B}.json"])
    assert rt.retouched(bucket, out, A) and rt.retouched(bucket, out, B) and not rt.retouched(bucket, out, C_)


def test_an_excluded_photo_is_never_published_whatever_chose_it():
    from deploy import standardise_publish as pub
    run = f"{ENV_SEGMENT}/standardise-test/full/sentas/"
    made = {f"{run}{A}.main.jpg", f"{run}{A}.white.jpg", f"{run}{B}.main.jpg"}
    chosen = [{"source": "sentas", "sha": A, "prefix": run}, {"source": "sentas", "sha": B, "prefix": run}]
    assert pub.copies(chosen, made, exclude={B}) == sorted(
        (f"{run}{A}.{v}.jpg", imagestore.standard_key("sentas", A, v)) for v in ("main", "white"))


def test_the_exclude_list_reads_one_sha_per_line_with_comments():
    from deploy import standardise_publish as pub
    text = f"# a cut-out that took in the floor\n{A}  # sentas, open book over the wall slabs\n\n"
    assert pub.read_exclude(text) == {A}


def test_a_whole_slab_leads_even_when_a_close_up_comes_first():
    views = {"main": {A, B}, "white": {A, B}, "studio": {B}, "close": {B}}     # A is a close-up: no side view
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", B), _std("white", B), _std("studio", B), _std("close", B)]


def test_a_lead_block_is_shown_once():
    views = {"main": {A}, "white": set(), "studio": set(), "close": set()}
    assert images.standardised([_improved(A)], views) == [_std("main", A)]


def test_a_marks_survey_notes_whether_a_photo_carries_lamp_reflections():
    import numpy as np
    from deploy import standardise_retouch as rt
    none = np.zeros((40, 60), np.uint8)
    lamp = none.copy(); lamp[10:14, 20:24] = 1
    assert rt.survey_note(None) == {"marks": False, "flares": False}
    assert rt.survey_note(tuple(rt._pack(m) for m in (none, lamp, none, none, none, none))) == {"marks": True, "flares": True}
    assert rt.survey_note(tuple(rt._pack(m) for m in (lamp, none, none, none, none, none))) == {"marks": True, "flares": False}


def test_a_run_can_be_limited_to_a_list_of_photos():
    from deploy import standardise_source as src
    keys = [f"dev/products/scraped/sentas/{s}.jpg" for s in (A, B, C_)]
    assert src.only(keys, None) == keys                                  # no list: every photo
    assert src.only(keys, [C_, A]) == [keys[0], keys[2]]                 # the listed ones, in the run's own order


def test_a_run_reads_its_environment_s_current_categories_before_its_product_sheet(monkeypatch):
    from deploy import fetch_inputs, standardise_source as src
    from stone_pipeline.config import settings
    calls = []
    monkeypatch.setattr(fetch_inputs, "fetch_attributes", lambda client: calls.append("fetch") or True)
    monkeypatch.setattr(settings, "refresh_category_pcats", lambda: calls.append("refresh"))
    monkeypatch.setattr(src, "_get", lambda client, key: calls.append(f"sheet {key}") or b"")
    monkeypatch.setattr(src.index, "build", lambda text: calls.append("index") or {})
    assert src.sheet_photos(object()) == {}
    assert calls == ["fetch", "refresh", f"sheet {settings.ENV_SEGMENT}/{src.C.SHEET_KEY}", "index"]   # prod has no committed export


def test_the_product_index_follows_the_categories_refreshed_from_the_export(monkeypatch):
    from dataclasses import replace
    from stone_pipeline.config import settings
    from stone_pipeline.standardise import index
    slab = replace(settings.category("slab"), pcat_id="pcat_PROD_SLABS")             # the id prod's export carries
    monkeypatch.setattr(settings, "_BY_NAME", {**settings._BY_NAME, slab.name: slab})
    sheet = ("Product Handle,Product Category 1,Product Length,Product Width,Product Image 1\n"
             f"h,pcat_PROD_SLABS,3.0,1.8,https://b.s3.amazonaws.com/prod/products/improved/sentas/{A}.jpg\n")
    assert index.build(sheet)[A]["format"] == "slab"


def test_listings_keep_each_product_s_photos_in_its_own_order_once_each(monkeypatch):
    from dataclasses import replace
    from stone_pipeline.config import settings
    from stone_pipeline.standardise import index
    slab = replace(settings.category("slab"), pcat_id="pcat_S")
    monkeypatch.setattr(settings, "_BY_NAME", {**settings._BY_NAME, slab.name: slab})
    url = "https://b.s3.amazonaws.com/dev/products/improved/sentas/{}.jpg"
    sheet = ("Product Handle,Product Category 1,Product Thumbnail,Product Image 1,Product Image 2\n"
             f"one,pcat_S,,{url.format(B)},{url.format(A)}\n"
             f"two,pcat_S,,{url.format(A)},{url.format(A)}\n"
             "none,pcat_S,,,\n")
    assert index.listings(sheet) == [{"key": "one", "source": "sentas", "format": "slab", "photos": [B, A]},
                                     {"key": "two", "source": "sentas", "format": "slab", "photos": [A]}]
