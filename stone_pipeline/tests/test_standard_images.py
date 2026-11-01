"""Products link to their standardised photos when a published set exists: the lead slab's showroom image first (the
catalog thumbnail), then for every slab in turn its white, side and close-up views (every slab of a lot is shown
whole); a product with no standardised photo keeps the images it had."""
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


def test_every_slab_of_a_product_shows_its_white_side_and_close_up_after_the_showroom():
    views = {"main": {A, B}, "white": {A, B}, "studio": {A, B}, "close": {A, B}}
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", A), _std("white", A), _std("studio", A), _std("close", A),
                   _std("white", B), _std("studio", B), _std("close", B)]


def test_an_unstandardised_photo_is_dropped_when_others_are_standardised():
    views = {"main": {B}, "white": {B}, "studio": set(), "close": set()}
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", B), _std("white", B)]


def test_a_product_with_no_standardised_photo_keeps_its_images():
    urls = [_improved(A), _improved(C_)]
    assert images.standardised(urls, {"main": {B}, "white": set(), "studio": set(), "close": set()}) == urls


def test_a_further_photo_with_no_gallery_view_shows_its_main_image():
    views = {"main": {A, B}, "white": {A}, "studio": set(), "close": set()}       # B is a block: one image only
    out = images.standardised([_improved(A, "zucchi"), _improved(B, "zucchi")], views)
    assert out == [_std("main", A, "zucchi"), _std("white", A, "zucchi"), _std("main", B, "zucchi")]


def test_a_finished_run_is_published_view_by_view_and_nothing_else():
    from deploy import standardise_publish as pub
    run = f"{ENV_SEGMENT}/standardise-test/full/sentas/"
    keys = [f"{run}{A}.main.jpg", f"{run}{A}.white.jpg", f"{run}{A}.studio.jpg", f"{run}{A}.close.jpg", f"{run}{A}.slab.png",
            f"{run}held/{B}.json", f"{run}retouched/{A}.jpg", f"{run}{B}.angle_left.jpg"]
    assert pub.copies("sentas", run, keys) == sorted(
        (f"{run}{A}.{v}.jpg", imagestore.standard_key("sentas", A, v)) for v in imagestore.STANDARD_VIEWS)


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


def test_an_excluded_photo_is_never_published():
    from deploy import standardise_publish as pub
    run = f"{ENV_SEGMENT}/standardise-test/full/sentas/"
    keys = [f"{run}{A}.main.jpg", f"{run}{A}.white.jpg", f"{run}{B}.main.jpg"]
    assert pub.copies("sentas", run, keys, exclude={B}) == sorted(
        (f"{run}{A}.{v}.jpg", imagestore.standard_key("sentas", A, v)) for v in ("main", "white"))


def test_the_exclude_list_reads_one_sha_per_line_with_comments():
    from deploy import standardise_publish as pub
    text = f"# a cut-out that took in the floor\n{A}  # sentas, open book over the wall slabs\n\n"
    assert pub.read_exclude(text) == {A}


def test_a_whole_slab_leads_even_when_a_close_up_comes_first():
    views = {"main": {A, B}, "white": {A, B}, "studio": {B}, "close": {B}}     # A is a close-up: no side view
    out = images.standardised([_improved(A), _improved(B)], views)
    assert out == [_std("main", B), _std("white", B), _std("studio", B), _std("close", B), _std("white", A)]   # A: a close-up, white only


def test_a_lead_block_is_shown_once():
    views = {"main": {A}, "white": set(), "studio": set(), "close": set()}
    assert images.standardised([_improved(A)], views) == [_std("main", A)]


def test_a_marks_survey_notes_whether_a_photo_carries_lamp_reflections():
    import numpy as np
    from deploy import standardise_retouch as rt
    none = np.zeros((40, 60), np.uint8)
    lamp = none.copy(); lamp[10:14, 20:24] = 1
    assert rt.survey_note(None) == {"marks": False, "flares": False}
    assert rt.survey_note(tuple(rt._pack(m) for m in (none, lamp, none, none, none))) == {"marks": True, "flares": True}
    assert rt.survey_note(tuple(rt._pack(m) for m in (lamp, none, none, none, none))) == {"marks": True, "flares": False}


def test_a_run_can_be_limited_to_a_list_of_photos():
    from deploy import standardise_source as src
    keys = [f"dev/products/scraped/sentas/{s}.jpg" for s in (A, B, C_)]
    assert src.only(keys, None) == keys                                  # no list: every photo
    assert src.only(keys, [C_, A]) == [keys[0], keys[2]]                 # the listed ones, in the run's own order
